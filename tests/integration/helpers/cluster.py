import base64
import concurrent
import errno
import http.client
import logging
import os
import os.path as p
import platform
import pprint
import pwd
import random
import re
import shlex
import shutil
import socket
import stat
import subprocess
import time
import traceback
import urllib.parse
import uuid
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any, List, Sequence, Tuple, Union

import requests
import urllib3

try:
    # Please, add modules that required for specific tests only here.
    # So contributors will be able to run most tests locally
    # without installing tons of unneeded packages that may be not so easy to install.
    import asyncio
    import ssl

    import cassandra.cluster
    import nats
    import psycopg2
    import pymongo
    import pymysql
    from cassandra.policies import RoundRobinPolicy
    from confluent_kafka.avro.cached_schema_registry_client import (
        CachedSchemaRegistryClient,
    )
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

except Exception as e:
    logging.warning(f"Cannot import some modules, some tests may not work: {e}")

import docker
from dict2xml import dict2xml
from docker.models.containers import Container
from kazoo.exceptions import KazooException
from minio import Minio

from . import pytest_xdist_logging_to_separate_files
from .client import Client, QueryRuntimeException
from .config_cluster import *
from .kazoo_client import KazooClientWithImplicitRetries
from .random_settings import write_random_settings_config
from .retry_decorator import retry
from .test_tools import assert_eq_with_retry, exec_query_with_retry

HELPERS_DIR = p.dirname(__file__)
CLICKHOUSE_ROOT_DIR = p.join(p.dirname(__file__), "../../..")
LOCAL_DOCKER_COMPOSE_DIR = p.join(CLICKHOUSE_ROOT_DIR, "tests/integration/compose/")
DEFAULT_ENV_NAME = ".env"
DEFAULT_BASE_CONFIG_DIR = os.environ.get(
    "CLICKHOUSE_TESTS_BASE_CONFIG_DIR", "/etc/clickhouse-server/"
)
DOCKER_BASE_TAG = os.environ.get("DOCKER_BASE_TAG", "latest")

SANITIZER_SIGN = "=================="

CLICKHOUSE_START_COMMAND = (
    "clickhouse server --config-file=/etc/clickhouse-server/{main_config_file}"
)

CLICKHOUSE_LOG_FILE = "/var/log/clickhouse-server/clickhouse-server.log"

CLICKHOUSE_ERROR_LOG_FILE = "/var/log/clickhouse-server/clickhouse-server.err.log"

# Minimum version we use in integration tests to check compatibility with old releases
# Keep in mind that we only support upgrading between releases that are at most 1 year different.
# This means that this minimum need to be, at least, 1 year older than the current release
CLICKHOUSE_CI_MIN_TESTED_VERSION = "23.3"

ZOOKEEPER_CONTAINERS = ("zoo1", "zoo2", "zoo3")


# to create docker-compose env file
def _create_env_file(path, variables):
    logging.debug("Env %s stored in %s", variables, path)
    with open(path, "w") as f:
        for var, value in list(variables.items()):
            f.write("=".join([var, value]) + "\n")
    return path


def run_and_check(
    args: Union[Sequence[str], str],
    env=None,
    shell=False,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=300,
    nothrow=False,
    detach=False,
) -> str:
    if shell:
        if isinstance(args, str):
            shell_args = args
        else:
            shell_args = next(a for a in args)
    else:
        shell_args = " ".join(args)

    logging.debug("Command:[%s]", shell_args)
    if detach:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            shell=shell,
        )
        return ""

    res = subprocess.run(
        args,
        stdout=stdout,
        stderr=stderr,
        env=env,
        shell=shell,
        timeout=timeout,
        check=False,
    )
    out = res.stdout.decode("utf-8", "ignore")
    err = res.stderr.decode("utf-8", "ignore")
    # check_call(...) from subprocess does not print stderr, so we do it manually
    for outline in out.splitlines():
        logging.debug("Stdout:%s", outline)
    for errline in err.splitlines():
        logging.debug("Stderr:%s", errline)
    if res.returncode != 0:
        logging.debug("Exitcode:%s", res.returncode)
        if env:
            logging.debug("Env:%s", env)
        if not nothrow:
            raise Exception(
                f"Command [{shell_args}] return non-zero code {res.returncode}: {res.stderr.decode('utf-8')}"
            )
    return out


def is_port_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", port))
            return True
    except socket.error:
        return False


class PortPoolManager:
    """
    This class is used for distribution of ports allocated to single pytest-xdist worker
    It can be used by multiple ClickHouseCluster instances
    """

    # Shared between instances
    all_ports = None
    free_ports = None

    def __init__(self):
        self.used_ports = []

        if self.all_ports is None:
            worker_ports = os.getenv("WORKER_FREE_PORTS")
            ports = [int(p) for p in worker_ports.split(" ")]

            # Static vars
            PortPoolManager.all_ports = ports
            PortPoolManager.free_ports = ports

    def get_port(self):
        for port in self.free_ports:
            if is_port_free(port):
                self.free_ports.remove(port)
                self.used_ports.append(port)
                return port

        raise Exception(
            f"No free ports: {self.all_ports}",
        )

    def return_used_ports(self):
        self.free_ports.extend(self.used_ports)
        self.used_ports.clear()


def docker_exec(*args: str) -> Tuple[str, ...]:
    "Function to ease the `docker exec -i...`"
    return ("docker", "exec", "-i", *args)


def retry_exception(num, delay, func, exception=Exception, *args, **kwargs):
    """
    Retry if `func()` throws, `num` times.

    :param func: func to run
    :param num: number of retries

    :throws StopIteration
    """
    i = 0
    while i <= num:
        try:
            func(*args, **kwargs)
            time.sleep(delay)
        except exception:  # pylint: disable=broad-except
            i += 1
            continue
        return
    raise StopIteration("Function did not finished successfully")


def subprocess_check_call(
    args: Union[Sequence[str], str],
    detach: bool = False,
    nothrow: bool = False,
    **kwargs,
) -> str:
    # Uncomment for debugging
    # logging.info('run:' + ' '.join(args))
    return run_and_check(args, detach=detach, nothrow=nothrow, **kwargs)


def get_docker_compose_path():
    return LOCAL_DOCKER_COMPOSE_DIR


def check_kafka_is_available(kafka_id, kafka_port):
    p = subprocess.Popen(
        docker_exec(
            kafka_id,
            "/usr/bin/kafka-broker-api-versions",
            "--bootstrap-server",
            f"INSIDE://localhost:{kafka_port}",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    p.communicate()
    return p.returncode == 0


def check_kerberos_kdc_is_available(kerberos_kdc_id):
    p = subprocess.Popen(
        docker_exec(kerberos_kdc_id, "/etc/rc.d/init.d/krb5kdc", "status"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    p.communicate()
    return p.returncode == 0


def check_postgresql_java_client_is_available(postgresql_java_client_id):
    p = subprocess.Popen(
        docker_exec(postgresql_java_client_id, "java", "-version"),
        stdout=subprocess.PIPE,
    )
    p.communicate()
    return p.returncode == 0


def run_rabbitmqctl(rabbitmq_id, cookie, command, timeout=90):
    try:
        subprocess.check_output(
            docker_exec(
                "-e",
                f"RABBITMQ_ERLANG_COOKIE={cookie}",
                rabbitmq_id,
                "rabbitmqctl",
                command,
            ),
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        # Raised if the command returns a non-zero exit code
        error_message = (
            f"rabbitmqctl {command} failed with return code {e.returncode}. "
            f"Output: {e.output.decode(errors='replace')}"
        )
        raise RuntimeError(error_message)
    except subprocess.TimeoutExpired as e:
        # Raised if the command times out
        output = (
            f". Output: {e.stdout.decode(errors='replace')}"
            if e.stdout is not None
            else ""
        )
        raise RuntimeError(f"rabbitmqctl {command} timed out{output}")


def check_rabbitmq_is_available(rabbitmq_id, cookie):
    run_rabbitmqctl(rabbitmq_id, cookie, "await_startup", 5)
    return True


def rabbitmq_debuginfo(rabbitmq_id, cookie):
    p = subprocess.Popen(
        docker_exec(
            "-e",
            f"RABBITMQ_ERLANG_COOKIE={cookie}",
            rabbitmq_id,
            "rabbitmq-diagnostics",
            "status",
        ),
        stdout=subprocess.PIPE,
    )
    p.communicate()

    p = subprocess.Popen(
        docker_exec(
            "-e",
            f"RABBITMQ_ERLANG_COOKIE={cookie}",
            rabbitmq_id,
            "rabbitmq-diagnostics",
            "listeners",
        ),
        stdout=subprocess.PIPE,
    )
    p.communicate()

    p = subprocess.Popen(
        docker_exec(
            "-e",
            f"RABBITMQ_ERLANG_COOKIE={cookie}",
            rabbitmq_id,
            "rabbitmq-diagnostics",
            "environment",
        ),
        stdout=subprocess.PIPE,
    )
    p.communicate()


async def check_nats_is_available(nats_port, ssl_ctx=None):
    nc = await nats_connect_ssl(
        nats_port,
        user="click",
        password="house",
        ssl_ctx=ssl_ctx,
        max_reconnect_attempts=1,
    )
    available = nc.is_connected
    await nc.close()
    return available


async def nats_connect_ssl(nats_port, user, password, ssl_ctx=None, **connect_options):
    if not ssl_ctx:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    nc = await nats.connect(
        "tls://localhost:{}".format(nats_port),
        user=user,
        password=password,
        tls=ssl_ctx,
        **connect_options,
    )
    return nc


def get_instances_dir(name):
    instances_dir_name = "_instances"

    run_id = os.environ.get("INTEGRATION_TESTS_RUN_ID", "")

    if name:
        instances_dir_name += "-" + name

    if run_id:
        instances_dir_name += "-" + shlex.quote(run_id)

    return instances_dir_name


def extract_test_name(base_path):
    """Extracts the name of the test based to a path to its test*.py file
    Must be unique in each test directory (because it's used to make instances dir and to stop docker containers from previous run)
    """
    name = p.basename(base_path)
    if name == "test.py":
        name = ""
    elif name.startswith("test_") and name.endswith(".py"):
        name = name[len("test_") : (len(name) - len(".py"))]
    return name


class ClickHouseCluster:
    """ClickHouse cluster with several instances and (possibly) ZooKeeper.

    Add instances with several calls to add_instance(), then start them with the start() call.

    Directories for instances are created in the directory of base_path. After cluster is started,
    these directories will contain logs, database files, docker-compose config, ClickHouse configs etc.
    """

    def __init__(
        self,
        base_path,
        name=None,
        base_config_dir=None,
        server_bin_path=None,
        client_bin_path=None,
        zookeeper_config_path=None,
        keeper_config_dir=None,
        custom_dockerd_host=None,
        zookeeper_keyfile=None,
        zookeeper_certfile=None,
        with_spark=False,
        custom_keeper_configs=[],
    ):
        for param in list(os.environ.keys()):
            logging.debug("ENV %40s %s" % (param, os.environ[param]))
        self.base_path = base_path
        self.base_dir = p.dirname(base_path)
        self.name = name if name is not None else extract_test_name(base_path)

        self.base_config_dir = base_config_dir or DEFAULT_BASE_CONFIG_DIR
        self.server_bin_path = p.realpath(
            server_bin_path
            or os.environ.get("CLICKHOUSE_TESTS_SERVER_BIN_PATH", "/usr/bin/clickhouse")
        )
        self.client_bin_path = p.realpath(
            client_bin_path
            or os.environ.get(
                "CLICKHOUSE_TESTS_CLIENT_BIN_PATH", "/usr/bin/clickhouse-client"
            )
        )
        self.zookeeper_config_path = (
            p.join(self.base_dir, zookeeper_config_path)
            if zookeeper_config_path
            else p.join(HELPERS_DIR, "zookeeper_config.xml")
        )

        self.keeper_config_dir = (
            p.join(self.base_dir, keeper_config_dir)
            if keeper_config_dir
            else HELPERS_DIR
        )

        self.custom_keeper_configs_paths = None
        if len(custom_keeper_configs) > 0:
            self.custom_keeper_configs_paths = [
                p.abspath(p.join(self.base_dir, c)) for c in custom_keeper_configs
            ]

        project_name = (
            pwd.getpwuid(os.getuid()).pw_name + p.basename(self.base_dir) + self.name
        )
        # docker-compose removes everything non-alphanumeric from project names so we do it too.
        self.project_name = re.sub(r"[^a-z0-9]", "", project_name.lower())
        self.instances_dir_name = get_instances_dir(self.name)
        xdist_worker = os.getenv("PYTEST_XDIST_WORKER")
        if xdist_worker:
            self.project_name += f"-{xdist_worker}"
            self.instances_dir_name += f"-{xdist_worker}"

        self.instances_dir = p.join(self.base_dir, self.instances_dir_name)
        self.docker_logs_path = p.join(self.instances_dir, "docker.log")
        self.env_file = p.join(self.instances_dir, DEFAULT_ENV_NAME)
        self.env_variables = {}
        # Problems with glibc 2.36+ [1]
        #
        #    [1]: https://github.com/ClickHouse/ClickHouse/issues/43426#issuecomment-1368512678
        self.env_variables["ASAN_OPTIONS"] = "use_sigaltstack=0"
        self.env_variables["TSAN_OPTIONS"] = "use_sigaltstack=0"
        self.env_variables["CLICKHOUSE_WATCHDOG_ENABLE"] = "0"
        self.env_variables["CLICKHOUSE_NATS_TLS_SECURE"] = "0"
        self.up_called = False

        custom_dockerd_host = custom_dockerd_host or os.environ.get(
            "CLICKHOUSE_TESTS_DOCKERD_HOST"
        )
        self.docker_api_version = os.environ.get("DOCKER_API_VERSION")

        self.docker_logs_proc = None  # type: Optional[subprocess.Popen]

        self.base_cmd = ["docker", "compose"]
        if custom_dockerd_host:
            self.base_cmd += ["--host", custom_dockerd_host]
        self.base_cmd += ["--env-file", self.env_file]
        self.base_cmd += ["--project-name", self.project_name]

        self.base_zookeeper_cmd = None
        self.base_minio_cmd = []
        self.base_mysql57_cmd = []
        self.base_mysql8_cmd = []
        self.base_kafka_cmd = []
        self.base_kafka_sasl_cmd = []
        self.base_kerberized_kafka_cmd = []
        self.base_kerberos_kdc_cmd = []
        self.base_rabbitmq_cmd = []
        self.base_nats_cmd = []
        self.base_cassandra_cmd = []
        self.base_jdbc_bridge_cmd = []
        self.base_postgres_cmd = []
        self.base_mongo_cmd = []
        self.base_redis_cmd = []
        self.base_azurite_cmd = []
        self.base_nginx_cmd = []
        self.pre_zookeeper_commands = []
        self.instances: dict[str, ClickHouseInstance] = {}
        self.with_zookeeper = False
        self.with_zookeeper_secure = False
        self.with_mysql_client = False
        self.with_mysql57 = False
        self.with_mysql8 = False
        self.with_mysql_cluster = False
        self.with_postgres = False
        self.with_postgres_cluster = False
        self.with_postgresql_java_client = False
        self.with_kafka = False
        self.with_kafka_sasl = False
        self.with_kerberized_kafka = False
        self.with_kerberos_kdc = False
        self.with_rabbitmq = False
        self.with_nats = False
        self.with_odbc_drivers = False
        self.with_mongo = False
        self.with_net_trics = False
        self.with_redis = False
        self.with_cassandra = False
        self.with_ldap = False
        self.with_jdbc_bridge = False
        self.with_nginx = False
        self.with_hive = False
        self.with_coredns = False

        # available when with_minio == True
        self.with_minio = False
        self.minio_dir = os.path.join(self.instances_dir, "minio")
        self.minio_certs_dir = None  # source for certificates
        self.minio_data_dir = p.join(self.minio_dir, "data")
        self.minio_host = "minio1"
        self.minio_ip = None
        self.minio_bucket = "root"
        self.minio_bucket_2 = "root2"
        self.minio_bucket_db_disk = "root-db-disk"
        self.minio_port = 9001
        self.minio_client = None  # type: Minio
        self.minio_redirect_host = "proxy1"
        self.minio_redirect_ip = None
        self.minio_redirect_port = 8080
        self.minio_docker_id = self.get_instance_docker_id(self.minio_host)
        self.resolver_logs_dir = os.path.join(self.instances_dir, "resolver")

        self.spark_session = None
        self.with_iceberg_catalog = False
        self.with_glue_catalog = False
        self.with_hms_catalog = False

        self.with_azurite = False
        self.azurite_container = "azurite-container"
        self.blob_service_client = None
        self._azurite_port = 0

        # available when with_kafka == True
        self.kafka_host = "kafka1"
        self.kafka_dir = os.path.join(self.instances_dir, "kafka")
        self._kafka_port = 0
        self.kafka_docker_id = None
        self.schema_registry_host = "schema-registry"
        self._schema_registry_port = 0
        self.schema_registry_auth_host = "schema-registry-auth"
        self._schema_registry_auth_port = 0
        self.kafka_docker_id = self.get_instance_docker_id(self.kafka_host)

        self.coredns_host = "coredns"

        self.kafka_sasl_host = "kafka_sasl"
        self.kafka_sasl_dir = os.path.join(self.instances_dir, "kafka_sasl")
        self._kafka_sasl_port = 0
        self.kafka_sasl_docker_id = None
        self.kafka_sasl_docker_id = self.get_instance_docker_id(self.kafka_sasl_host)

        # available when with_kerberozed_kafka == True
        # reuses kafka_dir
        self.kerberized_kafka_host = "kerberized_kafka1"
        self._kerberized_kafka_port = 0
        self.kerberized_kafka_docker_id = self.get_instance_docker_id(
            self.kerberized_kafka_host
        )

        # available when with_kerberos_kdc == True
        self.kerberos_kdc_host = "kerberoskdc"
        self.keberos_kdc_docker_id = self.get_instance_docker_id(self.kerberos_kdc_host)

        # available when with_mongo == True
        self.mongo_host = "mongo1"
        self._mongo_port = 0
        self.mongo_no_cred_host = "mongo_no_cred"
        self._mongo_no_cred_port = 0
        self.mongo_secure_host = "mongo_secure"
        self._mongo_secure_port = 0

        # available when with_cassandra == True
        self.cassandra_host = "cassandra1"
        self.cassandra_port = 9042
        self.cassandra_ip = None
        self.cassandra_id = self.get_instance_docker_id(self.cassandra_host)

        # available when with_ldap == True
        self.ldap_host = "openldap"
        self.ldap_container = None
        self.ldap_port = 1389
        self.ldap_id = self.get_instance_docker_id(self.ldap_host)

        # available when with_rabbitmq == True
        self.rabbitmq_host = "rabbitmq1"
        self.rabbitmq_ip = None
        self.rabbitmq_port = 5672
        self.rabbitmq_secure_port = 5671
        self.rabbitmq_dir = p.abspath(p.join(self.instances_dir, "rabbitmq"))
        self.rabbitmq_cookie_file = os.path.join(self.rabbitmq_dir, "erlang.cookie")
        self.rabbitmq_logs_dir = os.path.join(self.rabbitmq_dir, "logs")
        self.rabbitmq_cookie = self.get_instance_docker_id(self.rabbitmq_host)

        self.nats_host = "nats1"
        self.nats_port = 4444
        self.nats_docker_id = None
        self.nats_dir = p.abspath(p.join(self.instances_dir, "nats"))
        self.nats_cert_dir = os.path.join(self.nats_dir, "cert")
        self.nats_ssl_context = None

        # available when with_nginx == True
        self.nginx_host = "nginx"
        self.nginx_ip = None
        self._nginx_port = None
        self.nginx_id = self.get_instance_docker_id(self.nginx_host)

        # available when with_redis == True
        self.redis_host = "redis1"
        self._redis_port = 0

        # available when with_postgres == True
        self.postgres_host = "postgres1"
        self.postgres_ip = None
        self.postgres_conn = None
        self.postgres2_host = "postgres2"
        self.postgres2_ip = None
        self.postgres2_conn = None
        self.postgres3_host = "postgres3"
        self.postgres3_ip = None
        self.postgres3_conn = None
        self.postgres4_host = "postgres4"
        self.postgres4_ip = None
        self.postgres4_conn = None
        self.postgres_port = 5432
        self.postgres_dir = p.abspath(p.join(self.instances_dir, "postgres"))
        self.postgres_logs_dir = os.path.join(self.postgres_dir, "postgres1")
        self.postgres2_logs_dir = os.path.join(self.postgres_dir, "postgres2")
        self.postgres3_logs_dir = os.path.join(self.postgres_dir, "postgres3")
        self.postgres4_logs_dir = os.path.join(self.postgres_dir, "postgres4")
        self.postgres_id = self.get_instance_docker_id(self.postgres_host)

        # available when with_postgresql_java_client = True
        self.postgresql_java_client_host = "java"
        self.postgresql_java_client_docker_id = self.get_instance_docker_id(
            self.postgresql_java_client_host
        )

        # available when with_mysql_client == True
        self.mysql_client_host = "mysql_client"
        self.mysql_client_container = None

        # available when with_mysql57 == True
        self.mysql57_host = "mysql57"
        self.mysql57_port = 3306
        self.mysql57_ip = None
        self.mysql57_dir = p.abspath(p.join(self.instances_dir, "mysql"))
        self.mysql57_logs_dir = os.path.join(self.mysql57_dir, "logs")

        # available when with_mysql8 == True
        self.mysql8_host = "mysql80"
        self.mysql8_port = 3306
        self.mysql8_ip = None
        self.mysql8_dir = p.abspath(p.join(self.instances_dir, "mysql8"))
        self.mysql8_logs_dir = os.path.join(self.mysql8_dir, "logs")

        # available when with_mysql_cluster == True
        self.mysql2_host = "mysql2"
        self.mysql3_host = "mysql3"
        self.mysql4_host = "mysql4"
        self.mysql2_ip = None
        self.mysql3_ip = None
        self.mysql4_ip = None
        self.mysql_cluster_dir = p.abspath(p.join(self.instances_dir, "mysql"))
        self.mysql_cluster_logs_dir = os.path.join(self.mysql8_dir, "logs")

        # available when with_zookeper_secure == True
        self.zookeeper_secure_port = 2281
        self.zookeeper_keyfile = zookeeper_keyfile
        self.zookeeper_certfile = zookeeper_certfile

        # available when with_zookeper == True
        self.use_keeper = True
        self.zookeeper_port = 2181
        self.keeper_instance_dir_prefix = p.join(
            p.abspath(self.instances_dir), "keeper"
        )  # if use_keeper = True
        self.zookeeper_instance_dir_prefix = p.join(self.instances_dir, "zk")
        self.zookeeper_dirs_to_create = []

        # available when with_jdbc_bridge == True
        self.jdbc_bridge_host = "bridge1"
        self.jdbc_bridge_ip = None
        self.jdbc_bridge_port = 9019
        self.jdbc_driver_dir = p.abspath(p.join(self.instances_dir, "jdbc_driver"))
        self.jdbc_driver_logs_dir = os.path.join(self.jdbc_driver_dir, "logs")

        # available when with_prometheus == True
        self.with_prometheus = False
        self.prometheus_writer_host = "prometheus_writer"
        self.prometheus_writer_ip = None
        self.prometheus_writer_port = 9090
        self.prometheus_writer_logs_dir = p.abspath(
            p.join(self.instances_dir, "prometheus_writer/logs")
        )
        self.prometheus_reader_host = "prometheus_reader"
        self.prometheus_reader_ip = None
        self.prometheus_reader_port = 9091
        self.prometheus_reader_logs_dir = p.abspath(
            p.join(self.instances_dir, "prometheus_reader/logs")
        )
        self.prometheus_remote_write_handler_host = None
        self.prometheus_remote_write_handler_port = 9092
        self.prometheus_remote_write_handler_path = "/write"
        self.prometheus_remote_read_handler_host = None
        self.prometheus_remote_read_handler_port = 9092
        self.prometheus_remote_read_handler_path = "/read"

        self.docker_client: docker.DockerClient = None
        self.is_up = False
        self.env = os.environ.copy()
        logging.debug(f"CLUSTER INIT base_config_dir:{self.base_config_dir}")
        if p.exists(self.instances_dir):
            shutil.rmtree(self.instances_dir, ignore_errors=True)
            logging.debug(f"Removed :{self.instances_dir}")

        if with_spark:
            import pyspark

            # if you change packages, don't forget to update them in docker/test/integration/runner/dockerd-entrypoint.sh
            (
                pyspark.sql.SparkSession.builder.appName("spark_test")
                # The jars are now linked to "$SPARK_HOME/jars" and we don't
                # need packages to be downloaded once and once again
                # .config(
                #     "spark.jars.packages",
                #     "org.apache.hudi:hudi-spark3.3-bundle_2.12:0.13.0,io.delta:delta-core_2.12:2.2.0,org.apache.iceberg:iceberg-spark-runtime-3.3_2.12:1.1.0",
                # )
                .master("local")
                .getOrCreate()
                .stop()
            )

        self.port_pool = PortPoolManager()

    def compose_cmd(self, *args: str) -> List[str]:
        return ["docker", "compose", "--project-name", self.project_name, *args]

    @property
    def nginx_port(self):
        if self._nginx_port:
            return self._nginx_port
        self._nginx_port = self.port_pool.get_port()
        return self._nginx_port

    @property
    def kafka_port(self):
        if self._kafka_port:
            return self._kafka_port
        self._kafka_port = self.port_pool.get_port()
        return self._kafka_port

    @property
    def schema_registry_port(self):
        if self._schema_registry_port:
            return self._schema_registry_port
        self._schema_registry_port = self.port_pool.get_port()
        return self._schema_registry_port

    @property
    def schema_registry_auth_port(self):
        if self._schema_registry_auth_port:
            return self._schema_registry_auth_port
        self._schema_registry_auth_port = self.port_pool.get_port()
        return self._schema_registry_auth_port

    @property
    def kafka_sasl_port(self):
        if self._kafka_sasl_port:
            return self._kafka_sasl_port
        self._kafka_sasl_port = self.port_pool.get_port()
        return self._kafka_sasl_port

    @property
    def kerberized_kafka_port(self):
        if self._kerberized_kafka_port:
            return self._kerberized_kafka_port
        self._kerberized_kafka_port = self.port_pool.get_port()
        return self._kerberized_kafka_port

    @property
    def azurite_port(self):
        if self._azurite_port:
            return self._azurite_port
        self._azurite_port = self.port_pool.get_port()
        return self._azurite_port

    @property
    def mongo_port(self):
        if self._mongo_port:
            return self._mongo_port
        self._mongo_port = self.port_pool.get_port()
        return self._mongo_port

    @property
    def mongo_no_cred_port(self):
        if self._mongo_no_cred_port:
            return self._mongo_no_cred_port
        self._mongo_no_cred_port = self.port_pool.get_port()
        return self._mongo_no_cred_port

    @property
    def mongo_secure_port(self):
        if self._mongo_secure_port:
            return self._mongo_secure_port
        self._mongo_secure_port = self.port_pool.get_port()
        return self._mongo_secure_port

    @property
    def redis_port(self):
        if self._redis_port:
            return self._redis_port
        self._redis_port = self.port_pool.get_port()
        return self._redis_port

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.port_pool.return_used_ports()

    def print_all_docker_pieces(self):
        res_networks = subprocess.check_output(
            f"docker network ls --filter name='{self.project_name}*'",
            shell=True,
            universal_newlines=True,
        )
        logging.debug(
            f"Docker networks for project {self.project_name} are {res_networks}"
        )
        res_containers = subprocess.check_output(
            f"docker container ls -a --filter name='{self.project_name}*'",
            shell=True,
            universal_newlines=True,
        )
        logging.debug(
            f"Docker containers for project {self.project_name} are {res_containers}"
        )
        res_volumes = subprocess.check_output(
            f"docker volume ls --filter name='{self.project_name}*'",
            shell=True,
            universal_newlines=True,
        )
        logging.debug(
            f"Docker volumes for project {self.project_name} are {res_volumes}"
        )

    def cleanup(self):
        logging.debug("Cleanup called")
        self.print_all_docker_pieces()

        if (
            os.environ
            and "DISABLE_CLEANUP" in os.environ
            and os.environ["DISABLE_CLEANUP"] == "1"
        ):
            logging.warning("Cleanup is disabled")
            return

        # Just in case kill unstopped containers from previous launch
        try:
            unstopped_containers = self.get_running_containers()
            logging.debug(f"Unstopped containers: {unstopped_containers}")
            if len(unstopped_containers):
                logging.debug(
                    f"Trying to kill unstopped containers: {unstopped_containers}"
                )
                for id in unstopped_containers:
                    run_and_check(f"docker kill {id}", shell=True, nothrow=True)
                    run_and_check(f"docker rm {id}", shell=True, nothrow=True)
                unstopped_containers = self.get_running_containers()
                if unstopped_containers:
                    logging.debug(f"Left unstopped containers: {unstopped_containers}")
                else:
                    logging.debug(f"Unstopped containers killed.")
            else:
                logging.debug(f"No running containers for project: {self.project_name}")
        except Exception as ex:
            logging.debug(f"Got exception removing containers {str(ex)}")

        # # Just in case remove unused networks
        try:
            logging.debug("Trying to prune unused networks...")

            list_networks = subprocess.check_output(
                f"docker network ls -q --filter name='{self.project_name}'",
                shell=True,
                universal_newlines=True,
            ).splitlines()
            if list_networks:
                logging.debug(f"Trying to remove networks: {list_networks}")
                run_and_check(f"docker network rm {' '.join(list_networks)}")
                logging.debug(f"Networks removed: {list_networks}")
        except:
            pass

        # Remove unused images
        try:
            logging.debug("Trying to prune unused images...")

            run_and_check(["docker", "image", "prune", "-f"])
            logging.debug("Images pruned")
        except:
            pass

        # Remove unused volumes
        try:
            logging.debug("Trying to prune unused volumes...")

            result = run_and_check(["docker volume ls | wc -l"], shell=True)
            if int(result) > 1:
                run_and_check(["docker", "volume", "prune", "-f"])
            logging.debug(f"Volumes pruned: {result}")
        except:
            pass

    def get_docker_handle(self, docker_id) -> Container:
        exception = None
        for i in range(20):
            try:
                return self.docker_client.containers.get(docker_id)
            except Exception as ex:
                print("Got exception getting docker handle", str(ex))
                time.sleep(0.5)
                exception = ex
        raise exception

    def get_client_cmd(self):
        cmd = self.client_bin_path
        if p.basename(cmd) == "clickhouse":
            cmd += " client"
        return cmd

    # Returns the list of currently running docker containers corresponding to this ClickHouseCluster.
    def get_running_containers(self):
        # docker-compose names containers using the following formula:
        # container_name = project_name + '-' + instance_name + '-1'
        # We need to have "^/" and "$" in the "--filter name" option below to filter by exact name of the container, see
        # https://stackoverflow.com/questions/48767760/how-to-make-docker-container-ls-f-name-filter-by-exact-name
        filter_name = f"^/{self.project_name}-.*-1$"
        # We want the command "docker container list" to show only containers' ID and their names, separated by colon.
        format = "{{.ID}}:{{.Names}}"
        containers = run_and_check(
            f"docker container list --all --filter name='{filter_name}' --format '{format}'",
            shell=True,
        )
        containers = dict(line.split(":", 1) for line in containers.splitlines())
        return containers

    def copy_file_from_container_to_container(
        self, src_node, src_path, dst_node, dst_path
    ):
        fname = os.path.basename(src_path)
        run_and_check(
            [f"docker cp {src_node.docker_id}:{src_path} {self.instances_dir}"],
            shell=True,
        )
        run_and_check(
            [f"docker cp {self.instances_dir}/{fname} {dst_node.docker_id}:{dst_path}"],
            shell=True,
        )

    def setup_zookeeper_secure_cmd(
        self, instance, env_variables, docker_compose_yml_dir
    ):
        logging.debug("Setup ZooKeeper Secure")
        zookeeper_docker_compose_path = p.join(
            docker_compose_yml_dir, "docker_compose_zookeeper_secure.yml"
        )
        env_variables["ZOO_SECURE_CLIENT_PORT"] = str(self.zookeeper_secure_port)
        env_variables["ZK_FS"] = "bind"
        for i in range(1, 4):
            zk_data_path = os.path.join(
                self.zookeeper_instance_dir_prefix + str(i), "data"
            )
            zk_log_path = os.path.join(
                self.zookeeper_instance_dir_prefix + str(i), "log"
            )
            env_variables["ZK_DATA" + str(i)] = zk_data_path
            env_variables["ZK_DATA_LOG" + str(i)] = zk_log_path
            self.zookeeper_dirs_to_create += [zk_data_path, zk_log_path]
            logging.debug(f"DEBUG ZK: {self.zookeeper_dirs_to_create}")

        self.with_zookeeper_secure = True
        self.base_cmd.extend(["--file", zookeeper_docker_compose_path])
        self.base_zookeeper_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            zookeeper_docker_compose_path,
        )
        return self.base_zookeeper_cmd

    def setup_zookeeper_cmd(self, instance, env_variables, docker_compose_yml_dir):
        logging.debug("Setup ZooKeeper")
        zookeeper_docker_compose_path = p.join(
            docker_compose_yml_dir, "docker_compose_zookeeper.yml"
        )

        env_variables["ZK_FS"] = "bind"
        for i in range(1, 4):
            zk_data_path = os.path.join(
                self.zookeeper_instance_dir_prefix + str(i), "data"
            )
            zk_log_path = os.path.join(
                self.zookeeper_instance_dir_prefix + str(i), "log"
            )
            env_variables["ZK_DATA" + str(i)] = zk_data_path
            env_variables["ZK_DATA_LOG" + str(i)] = zk_log_path
            self.zookeeper_dirs_to_create += [zk_data_path, zk_log_path]
            logging.debug(f"DEBUG ZK: {self.zookeeper_dirs_to_create}")

        self.with_zookeeper = True
        self.base_cmd.extend(["--file", zookeeper_docker_compose_path])
        self.base_zookeeper_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            zookeeper_docker_compose_path,
        )
        return self.base_zookeeper_cmd

    def setup_keeper_cmd(self, instance, env_variables, docker_compose_yml_dir):
        logging.debug("Setup Keeper")
        keeper_docker_compose_path = p.join(
            docker_compose_yml_dir, "docker_compose_keeper.yml"
        )

        binary_path = self.server_bin_path
        binary_dir = os.path.dirname(self.server_bin_path)

        # always prefer clickhouse-keeper standalone binary
        if os.path.exists(
            os.path.join(binary_dir, "clickhouse-keeper")
        ) and not os.path.islink(os.path.join(binary_dir, "clickhouse-keeper")):
            binary_path = os.path.join(binary_dir, "clickhouse-keeper")
            keeper_cmd_prefix = "clickhouse-keeper"
        else:
            if binary_path.endswith("-server"):
                binary_path = binary_path[: -len("-server")]
            keeper_cmd_prefix = "clickhouse keeper"

        env_variables["keeper_binary"] = binary_path
        env_variables["keeper_cmd_prefix"] = keeper_cmd_prefix
        env_variables["image"] = "clickhouse/integration-test:" + DOCKER_BASE_TAG
        env_variables["user"] = str(os.getuid())
        env_variables["keeper_fs"] = "bind"
        for i in range(1, 4):
            keeper_instance_dir = self.keeper_instance_dir_prefix + f"{i}"
            logs_dir = os.path.join(keeper_instance_dir, "log")
            configs_dir = os.path.join(keeper_instance_dir, "config")
            coordination_dir = os.path.join(keeper_instance_dir, "coordination")
            env_variables[f"keeper_logs_dir{i}"] = logs_dir
            env_variables[f"keeper_config_dir{i}"] = configs_dir
            env_variables[f"keeper_db_dir{i}"] = coordination_dir
            self.zookeeper_dirs_to_create += [logs_dir, configs_dir, coordination_dir]

        self.with_zookeeper = True
        self.base_cmd.extend(["--file", keeper_docker_compose_path])
        self.base_zookeeper_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            keeper_docker_compose_path,
        )
        return self.base_zookeeper_cmd

    def setup_mysql_client_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_mysql_client = True
        self.base_cmd.extend(
            [
                "--file",
                p.join(docker_compose_yml_dir, "docker_compose_mysql_client.yml"),
            ]
        )
        self.base_mysql_client_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_mysql_client.yml"),
        )

        return self.base_mysql_client_cmd

    def setup_mysql57_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_mysql57 = True
        env_variables["MYSQL_HOST"] = self.mysql57_host
        env_variables["MYSQL_PORT"] = str(self.mysql57_port)
        env_variables["MYSQL_ROOT_HOST"] = "%"
        env_variables["MYSQL_LOGS"] = self.mysql57_logs_dir
        env_variables["MYSQL_LOGS_FS"] = "bind"
        env_variables["MYSQL_DOCKER_USER"] = str(os.getuid())

        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_mysql.yml")]
        )
        self.base_mysql57_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_mysql.yml"),
        )

        return self.base_mysql57_cmd

    def setup_mysql8_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_mysql8 = True
        env_variables["MYSQL8_HOST"] = self.mysql8_host
        env_variables["MYSQL8_PORT"] = str(self.mysql8_port)
        env_variables["MYSQL8_ROOT_HOST"] = "%"
        env_variables["MYSQL8_LOGS"] = self.mysql8_logs_dir
        env_variables["MYSQL8_LOGS_FS"] = "bind"
        env_variables["MYSQL8_DOCKER_USER"] = str(os.getuid())

        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_mysql_8_0.yml")]
        )
        self.base_mysql8_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_mysql_8_0.yml"),
        )

        return self.base_mysql8_cmd

    def setup_mysql_cluster_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_mysql_cluster = True
        env_variables["MYSQL_CLUSTER_PORT"] = str(self.mysql8_port)
        env_variables["MYSQL_CLUSTER_ROOT_HOST"] = "%"
        env_variables["MYSQL_CLUSTER_LOGS"] = self.mysql_cluster_logs_dir
        env_variables["MYSQL_CLUSTER_LOGS_FS"] = "bind"
        env_variables["MYSQL_CLUSTER_DOCKER_USER"] = str(os.getuid())

        self.base_cmd.extend(
            [
                "--file",
                p.join(docker_compose_yml_dir, "docker_compose_mysql_cluster.yml"),
            ]
        )
        self.base_mysql_cluster_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_mysql_cluster.yml"),
        )

        return self.base_mysql_cluster_cmd

    def setup_postgres_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_postgres.yml")]
        )
        env_variables["POSTGRES_PORT"] = str(self.postgres_port)
        env_variables["POSTGRES_DIR"] = self.postgres_logs_dir
        env_variables["POSTGRES_LOGS_FS"] = "bind"

        self.with_postgres = True
        self.base_postgres_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_postgres.yml"),
        )
        return self.base_postgres_cmd

    def setup_postgres_cluster_cmd(
        self, instance, env_variables, docker_compose_yml_dir
    ):
        self.with_postgres_cluster = True
        env_variables["POSTGRES_PORT"] = str(self.postgres_port)
        env_variables["POSTGRES2_DIR"] = self.postgres2_logs_dir
        env_variables["POSTGRES3_DIR"] = self.postgres3_logs_dir
        env_variables["POSTGRES4_DIR"] = self.postgres4_logs_dir
        env_variables["POSTGRES_LOGS_FS"] = "bind"
        self.base_cmd.extend(
            [
                "--file",
                p.join(docker_compose_yml_dir, "docker_compose_postgres_cluster.yml"),
            ]
        )
        self.base_postgres_cluster_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_postgres_cluster.yml"),
        )

    def setup_postgresql_java_client_cmd(
        self, instance, env_variables, docker_compose_yml_dir
    ):
        self.with_postgresql_java_client = True
        self.base_cmd.extend(
            [
                "--file",
                p.join(
                    docker_compose_yml_dir, "docker_compose_postgresql_java_client.yml"
                ),
            ]
        )
        self.base_postgresql_java_client_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_postgresql_java_client.yml"),
        )

    def setup_kafka_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_kafka = True
        env_variables["KAFKA_HOST"] = self.kafka_host
        env_variables["KAFKA_EXTERNAL_PORT"] = str(self.kafka_port)
        env_variables["SCHEMA_REGISTRY_DIR"] = instance.path + "/"
        env_variables["SCHEMA_REGISTRY_EXTERNAL_PORT"] = str(self.schema_registry_port)
        env_variables["SCHEMA_REGISTRY_AUTH_EXTERNAL_PORT"] = str(
            self.schema_registry_auth_port
        )
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_kafka.yml")]
        )
        self.base_kafka_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_kafka.yml"),
        )
        return self.base_kafka_cmd

    def setup_kafka_sasl_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_kafka_sasl = True
        env_variables["KAFKA_HOST"] = self.kafka_sasl_host
        env_variables["KAFKA_EXTERNAL_PORT"] = str(self.kafka_sasl_port)
        env_variables["KAFKA_DIR"] = instance.path + "/"
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_kafka_sasl.yml")]
        )
        self.base_kafka_sasl_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_kafka_sasl.yml"),
        )
        return self.base_kafka_sasl_cmd

    def setup_kerberized_kafka_cmd(
        self, instance, env_variables, docker_compose_yml_dir
    ):
        self.with_kerberized_kafka = True
        env_variables["KERBERIZED_KAFKA_DIR"] = instance.path + "/"
        env_variables["KERBERIZED_KAFKA_HOST"] = self.kerberized_kafka_host
        env_variables["KERBERIZED_KAFKA_EXTERNAL_PORT"] = str(
            self.kerberized_kafka_port
        )
        self.base_cmd.extend(
            [
                "--file",
                p.join(docker_compose_yml_dir, "docker_compose_kerberized_kafka.yml"),
            ]
        )
        self.base_kerberized_kafka_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_kerberized_kafka.yml"),
        )
        return self.base_kerberized_kafka_cmd

    def setup_kerberos_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_kerberos_kdc = True
        env_variables["KERBEROS_KDC_DIR"] = self.instances_dir + "/"
        env_variables["KERBEROS_KDC_HOST"] = self.kerberos_kdc_host
        self.base_cmd.extend(
            [
                "--file",
                p.join(docker_compose_yml_dir, "docker_compose_kerberos_kdc.yml"),
            ]
        )
        self.base_kerberos_kdc_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_kerberos_kdc.yml"),
        )
        return self.base_kerberos_kdc_cmd

    def setup_redis_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_redis = True
        env_variables["REDIS_HOST"] = self.redis_host
        env_variables["REDIS_EXTERNAL_PORT"] = str(self.redis_port)
        env_variables["REDIS_INTERNAL_PORT"] = "6379"

        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_redis.yml")]
        )
        self.base_redis_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_redis.yml"),
        )
        return self.base_redis_cmd

    def setup_rabbitmq_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_rabbitmq = True
        env_variables["RABBITMQ_HOST"] = self.rabbitmq_host
        env_variables["RABBITMQ_PORT"] = str(self.rabbitmq_port)
        env_variables["RABBITMQ_SECURE_PORT"] = str(self.rabbitmq_secure_port)
        env_variables["RABBITMQ_LOGS"] = self.rabbitmq_logs_dir
        env_variables["RABBITMQ_LOGS_FS"] = "bind"
        env_variables["RABBITMQ_COOKIE_FILE"] = self.rabbitmq_cookie_file
        env_variables["RABBITMQ_COOKIE_FILE_FS"] = "bind"

        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_rabbitmq.yml")]
        )
        self.base_rabbitmq_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_rabbitmq.yml"),
        )
        return self.base_rabbitmq_cmd

    def setup_nats_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_nats = True
        env_variables["NATS_HOST"] = self.nats_host
        env_variables["NATS_INTERNAL_PORT"] = "4444"
        env_variables["NATS_EXTERNAL_PORT"] = str(self.nats_port)
        env_variables["NATS_CERT_DIR"] = self.nats_cert_dir

        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_nats.yml")]
        )
        self.base_nats_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_nats.yml"),
        )
        return self.base_nats_cmd

    def setup_mongo_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_mongo = True
        env_variables["MONGO_HOST"] = self.mongo_host
        env_variables["MONGO_EXTERNAL_PORT"] = str(self.mongo_port)
        env_variables["MONGO_INTERNAL_PORT"] = "27017"
        env_variables["MONGO_NO_CRED_EXTERNAL_PORT"] = str(self.mongo_no_cred_port)
        env_variables["MONGO_NO_CRED_INTERNAL_PORT"] = "27017"
        env_variables["MONGO_SECURE_EXTERNAL_PORT"] = str(self.mongo_secure_port)
        env_variables["MONGO_SECURE_INTERNAL_PORT"] = "27017"
        env_variables["MONGO_SECURE_CONFIG_DIR"] = (
            instance.path + "/" + "mongo_secure_config"
        )
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_mongo.yml")]
        )
        self.base_mongo_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_mongo.yml"),
        )
        return self.base_mongo_cmd

    def setup_coredns_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_coredns = True
        env_variables["COREDNS_CONFIG_DIR"] = instance.path + "/" + "coredns_config"
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_coredns.yml")]
        )

        self.base_coredns_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_coredns.yml"),
        )

        return self.base_coredns_cmd

    def setup_minio_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_minio = True
        cert_d = p.join(self.minio_dir, "certs")
        env_variables["MINIO_CERTS_DIR"] = cert_d
        env_variables["MINIO_DATA_DIR"] = self.minio_data_dir
        env_variables["MINIO_PORT"] = str(self.minio_port)
        env_variables["SSL_CERT_FILE"] = p.join(self.base_dir, cert_d, "public.crt")
        env_variables["RESOLVER_LOGS"] = self.resolver_logs_dir
        env_variables["RESOLVER_LOGS_FS"] = "bind"

        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_minio.yml")]
        )
        self.base_minio_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_minio.yml"),
        )
        return self.base_minio_cmd

    def setup_glue_catalog_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_glue_catalog = True
        self.base_cmd.extend(
            [
                "--file",
                p.join(docker_compose_yml_dir, "docker_compose_glue_catalog.yml"),
            ]
        )
        self.base_glue_catalog_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_glue_catalog.yml"),
        )
        return self.base_glue_catalog_cmd

    def setup_hms_catalog_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_hms_catalog = True
        self.base_cmd.extend(
            [
                "--file",
                p.join(
                    docker_compose_yml_dir, "docker_compose_iceberg_hms_catalog.yml"
                ),
            ]
        )

        self.base_iceberg_hms_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_iceberg_hms_catalog.yml"),
        )
        return self.base_iceberg_hms_cmd

    def setup_iceberg_catalog_cmd(
        self, instance, env_variables, docker_compose_yml_dir, extra_parameters=None
    ):
        self.with_iceberg_catalog = True
        file_name = "docker_compose_iceberg_rest_catalog.yml"
        if extra_parameters is not None and extra_parameters["docker_compose_file_name"] != "":
            file_name = extra_parameters["docker_compose_file_name"]
        self.base_cmd.extend(
            [
                "--file",
                p.join(
                    docker_compose_yml_dir, file_name
                ),
            ]
        )
        self.base_iceberg_catalog_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, file_name),
        )
        return self.base_iceberg_catalog_cmd

    def setup_azurite_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_azurite = True
        env_variables["AZURITE_PORT"] = str(self.azurite_port)
        env_variables["AZURITE_STORAGE_ACCOUNT_URL"] = (
            f"http://azurite1:{env_variables['AZURITE_PORT']}/devstoreaccount1"
        )
        env_variables["AZURITE_CONNECTION_STRING"] = (
            f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            f"AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
            f"BlobEndpoint={env_variables['AZURITE_STORAGE_ACCOUNT_URL']};"
        )

        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_azurite.yml")]
        )
        self.base_azurite_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_azurite.yml"),
        )
        return self.base_azurite_cmd

    def setup_cassandra_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_cassandra = True
        env_variables["CASSANDRA_PORT"] = str(self.cassandra_port)
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_cassandra.yml")]
        )
        self.base_cassandra_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_cassandra.yml"),
        )
        return self.base_cassandra_cmd

    def setup_ldap_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_ldap = True
        env_variables["LDAP_EXTERNAL_PORT"] = str(self.ldap_port)
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_ldap.yml")]
        )
        self.base_ldap_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_ldap.yml"),
        )
        return self.base_ldap_cmd

    def setup_jdbc_bridge_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_jdbc_bridge = True
        env_variables["JDBC_DRIVER_LOGS"] = self.jdbc_driver_logs_dir
        env_variables["JDBC_DRIVER_FS"] = "bind"
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_jdbc_bridge.yml")]
        )
        self.base_jdbc_bridge_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_jdbc_bridge.yml"),
        )
        return self.base_jdbc_bridge_cmd

    def setup_nginx_cmd(self, instance, env_variables, docker_compose_yml_dir):
        self.with_nginx = True

        env_variables["NGINX_EXTERNAL_PORT"] = str(self.nginx_port)
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_nginx.yml")]
        )
        self.base_nginx_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_nginx.yml"),
        )
        return self.base_nginx_cmd

    def setup_hive(self, instance, env_variables, docker_compose_yml_dir):
        self.with_hive = True
        self.base_cmd.extend(
            ["--file", p.join(docker_compose_yml_dir, "docker_compose_hive.yml")]
        )
        self.base_hive_cmd = self.compose_cmd(
            "--env-file",
            instance.env_file,
            "--file",
            p.join(docker_compose_yml_dir, "docker_compose_hive.yml"),
        )
        return self.base_hive_cmd

    def setup_prometheus_cmd(self, instance, env_variables, docker_compose_yml_dir):
        env_variables["PROMETHEUS_WRITER_HOST"] = self.prometheus_writer_host
        env_variables["PROMETHEUS_WRITER_PORT"] = str(self.prometheus_writer_port)
        env_variables["PROMETHEUS_WRITER_LOGS"] = self.prometheus_writer_logs_dir
        env_variables["PROMETHEUS_WRITER_LOGS_FS"] = "bind"
        env_variables["PROMETHEUS_READER_HOST"] = self.prometheus_reader_host
        env_variables["PROMETHEUS_READER_PORT"] = str(self.prometheus_reader_port)
        env_variables["PROMETHEUS_READER_LOGS"] = self.prometheus_reader_logs_dir
        env_variables["PROMETHEUS_READER_LOGS_FS"] = "bind"
        if self.prometheus_remote_write_handler_host:
            env_variables["PROMETHEUS_REMOTE_WRITE_HANDLER"] = (
                f"http://{self.prometheus_remote_write_handler_host}:{self.prometheus_remote_write_handler_port}/{self.prometheus_remote_write_handler_path.strip('/')}"
            )
        if self.prometheus_remote_read_handler_host:
            env_variables["PROMETHEUS_REMOTE_READ_HANDLER"] = (
                f"http://{self.prometheus_remote_read_handler_host}:{self.prometheus_remote_read_handler_port}/{self.prometheus_remote_read_handler_path.strip('/')}"
            )
        if not self.with_prometheus:
            self.with_prometheus = True
            self.base_cmd.extend(
                [
                    "--file",
                    p.join(docker_compose_yml_dir, "docker_compose_prometheus.yml"),
                ]
            )
            self.base_prometheus_cmd = self.compose_cmd(
                "--env-file",
                instance.env_file,
                "--file",
                p.join(docker_compose_yml_dir, "docker_compose_prometheus.yml"),
            )
        return self.base_prometheus_cmd

    def add_instance(
        self,
        name,
        base_config_dir=None,
        main_configs=None,
        user_configs=None,
        dictionaries=None,
        macros=None,
        with_zookeeper=False,
        with_zookeeper_secure=False,
        with_mysql_client=False,
        with_mysql57=False,
        with_mysql8=False,
        with_mysql_cluster=False,
        with_kafka=False,
        with_kafka_sasl=False,
        with_kerberized_kafka=False,
        with_kerberos_kdc=False,
        with_secrets=False,
        with_rabbitmq=False,
        with_nats=False,
        clickhouse_path_dir=None,
        with_odbc_drivers=False,
        with_postgres=False,
        with_postgres_cluster=False,
        with_postgresql_java_client=False,
        clickhouse_log_file=CLICKHOUSE_LOG_FILE,
        clickhouse_error_log_file=CLICKHOUSE_ERROR_LOG_FILE,
        with_mongo=False,
        with_nginx=False,
        with_redis=False,
        with_minio=False,
        # The config is defined in tests/integration/helpers/remote_database_disk.xml
        # However, some tests cannot use with_remote_database_disk by their configs: e.g using secure keeper
        # So, we set the default value of with_remote_database_disk to None and try to enable it if possible in DEBUG and ASAN build (i.e. if not explicitly set to false)
        with_remote_database_disk=None,
        with_azurite=False,
        with_cassandra=False,
        with_ldap=False,
        with_jdbc_bridge=False,
        with_hive=False,
        with_coredns=False,
        with_prometheus=False,
        with_iceberg_catalog=False,
        with_glue_catalog=False,
        with_hms_catalog=False,
        handle_prometheus_remote_write=False,
        handle_prometheus_remote_read=False,
        use_old_analyzer=None,
        use_distributed_plan=None,
        hostname=None,
        env_variables=None,
        instance_env_variables=False,
        image="clickhouse/integration-test",
        tag=None,
        # keep the docker container running when clickhouse server is stopped
        stay_alive=False,
        ipv4_address=None,
        ipv6_address=None,
        with_installed_binary=False,
        external_dirs=None,
        tmpfs=None,
        mem_limit=None,
        zookeeper_docker_compose_path=None,
        minio_certs_dir=None,
        minio_data_dir=None,
        use_keeper=True,
        keeper_randomize_feature_flags=True,
        keeper_required_feature_flags=[],
        main_config_name="config.xml",
        users_config_name="users.xml",
        copy_common_configs=True,
        config_root_name="clickhouse",
        extra_configs=[],
        extra_args="",
        randomize_settings=True,
        use_docker_init_flag=False,
        clickhouse_start_cmd=CLICKHOUSE_START_COMMAND,
        with_dolor=False,
        extra_parameters=None,
    ) -> "ClickHouseInstance":
        """Add an instance to the cluster.

        name - the name of the instance directory and the value of the 'instance' macro in ClickHouse.
        base_config_dir - a directory with config.xml and users.xml files which will be copied to /etc/clickhouse-server/ directory
        main_configs - a list of config files that will be added to config.d/ directory
        user_configs - a list of config files that will be added to users.d/ directory
        with_zookeeper - if True, add ZooKeeper configuration to configs and ZooKeeper instances to the cluster.
        with_zookeeper_secure - if True, add ZooKeeper Secure configuration to configs and ZooKeeper instances to the cluster.
        extra_configs - config files cannot put into config.d and users.d
        """

        if self.is_up:
            raise Exception("Can't add instance %s: cluster is already up!" % name)

        if name in self.instances:
            raise Exception(
                f"Can't add instance '{name}': there is already an instance with the same name in [{self.instances.keys()}]"
            )

        if tag is None:
            tag = DOCKER_BASE_TAG
        else:
            if with_remote_database_disk:
                raise Exception(
                    f"Can't add instance '{name}': not support remote database disk with the old version {tag}"
                )
            with_remote_database_disk = False

        if with_remote_database_disk is None:
            build_opts = subprocess.check_output(
                f"""{self.server_bin_path} local -q "SELECT value FROM system.build_options WHERE name = 'CXX_FLAGS'" """,
                stderr=subprocess.STDOUT,
                shell=True,
            ).decode()
            with_remote_database_disk = ("NDEBUG" not in build_opts) and (
                "-fsanitize=address" in build_opts
            )

        if with_remote_database_disk:
            logging.debug(f"Instance {name}, with_remote_database_disk enabled")
            with_minio = True

        if not env_variables:
            env_variables = {}
        self.use_keeper = use_keeper
        self.keeper_randomize_feature_flags = keeper_randomize_feature_flags
        self.keeper_required_feature_flags = keeper_required_feature_flags

        # Code coverage files will be placed in database directory
        # (affect only WITH_COVERAGE=1 build)
        env_variables["LLVM_PROFILE_FILE"] = (
            "/var/lib/clickhouse/server_%h_%p_%m.profraw"
        )

        clickhouse_start_command = clickhouse_start_cmd
        if clickhouse_log_file:
            clickhouse_start_command += " --log-file=" + clickhouse_log_file
        if clickhouse_error_log_file:
            clickhouse_start_command += " --errorlog-file=" + clickhouse_error_log_file
        logging.debug(f"clickhouse_start_command: {clickhouse_start_command}")

        instance = ClickHouseInstance(
            cluster=self,
            base_path=self.base_dir,
            name=name,
            base_config_dir=(
                base_config_dir if base_config_dir else self.base_config_dir
            ),
            custom_main_configs=main_configs or [],
            custom_user_configs=user_configs or [],
            custom_dictionaries=dictionaries or [],
            macros=macros or {},
            with_zookeeper=with_zookeeper,
            zookeeper_config_path=self.zookeeper_config_path,
            with_mysql_client=with_mysql_client,
            with_mysql57=with_mysql57,
            with_mysql8=with_mysql8,
            with_mysql_cluster=with_mysql_cluster,
            with_kafka=with_kafka,
            with_kafka_sasl=with_kafka_sasl,
            with_kerberized_kafka=with_kerberized_kafka,
            with_kerberos_kdc=with_kerberos_kdc,
            with_rabbitmq=with_rabbitmq,
            with_nats=with_nats,
            with_nginx=with_nginx,
            with_secrets=with_secrets
            or with_kerberos_kdc
            or with_kerberized_kafka
            or with_kafka_sasl,
            with_mongo=with_mongo,
            with_redis=with_redis,
            with_minio=with_minio,
            with_remote_database_disk=with_remote_database_disk,
            with_azurite=with_azurite,
            with_jdbc_bridge=with_jdbc_bridge,
            with_hive=with_hive,
            with_coredns=with_coredns,
            with_cassandra=with_cassandra,
            with_ldap=with_ldap,
            with_iceberg_catalog=with_iceberg_catalog,
            with_glue_catalog=with_glue_catalog,
            with_hms_catalog=with_hms_catalog,
            use_old_analyzer=use_old_analyzer,
            use_distributed_plan=use_distributed_plan,
            server_bin_path=self.server_bin_path,
            clickhouse_path_dir=clickhouse_path_dir,
            with_odbc_drivers=with_odbc_drivers,
            with_postgres=with_postgres,
            with_postgres_cluster=with_postgres_cluster,
            with_postgresql_java_client=with_postgresql_java_client,
            clickhouse_start_command=clickhouse_start_command,
            clickhouse_start_extra_args=extra_args,
            main_config_name=main_config_name,
            users_config_name=users_config_name,
            copy_common_configs=copy_common_configs,
            hostname=hostname,
            env_variables=env_variables,
            instance_env_variables=instance_env_variables,
            image=image,
            tag=tag,
            stay_alive=stay_alive,
            ipv4_address=ipv4_address,
            ipv6_address=ipv6_address,
            with_installed_binary=with_installed_binary,
            external_dirs=external_dirs,
            tmpfs=tmpfs or [],
            mem_limit=mem_limit,
            config_root_name=config_root_name,
            extra_configs=extra_configs,
            randomize_settings=randomize_settings,
            use_docker_init_flag=use_docker_init_flag,
            with_dolor=with_dolor,
            extra_parameters=extra_parameters,
        )

        docker_compose_yml_dir = get_docker_compose_path()
        docker_compose_net = p.join(docker_compose_yml_dir, "docker_compose_net.yml")

        self.instances[name] = instance
        if not self.with_net_trics and (
            ipv4_address is not None or ipv6_address is not None
        ):
            # docker compose v2 does not accept more than one argument `-f net.yml`
            self.with_net_trics = True
            self.base_cmd.extend(["--file", docker_compose_net])

        self.base_cmd.extend(["--file", instance.docker_compose_path])

        cmds = []
        if with_zookeeper_secure and not self.with_zookeeper_secure:
            cmds.append(
                self.setup_zookeeper_secure_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_zookeeper and not self.with_zookeeper:
            if self.use_keeper:
                cmds.append(
                    self.setup_keeper_cmd(
                        instance, env_variables, docker_compose_yml_dir
                    )
                )
            else:
                cmds.append(
                    self.setup_zookeeper_cmd(
                        instance, env_variables, docker_compose_yml_dir
                    )
                )

        if with_mysql_client and not self.with_mysql_client:
            cmds.append(
                self.setup_mysql_client_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_mysql57 and not self.with_mysql57:
            cmds.append(
                self.setup_mysql57_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_mysql8 and not self.with_mysql8:
            cmds.append(
                self.setup_mysql8_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_mysql_cluster and not self.with_mysql_cluster:
            cmds.append(
                self.setup_mysql_cluster_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_postgres and not self.with_postgres:
            cmds.append(
                self.setup_postgres_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_postgres_cluster and not self.with_postgres_cluster:
            cmds.append(
                self.setup_postgres_cluster_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_postgresql_java_client and not self.with_postgresql_java_client:
            cmds.append(
                self.setup_postgresql_java_client_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_odbc_drivers and not self.with_odbc_drivers:
            self.with_odbc_drivers = True
            if not self.with_mysql8:
                cmds.append(
                    self.setup_mysql8_cmd(
                        instance, env_variables, docker_compose_yml_dir
                    )
                )

            if not self.with_postgres:
                cmds.append(
                    self.setup_postgres_cmd(
                        instance, env_variables, docker_compose_yml_dir
                    )
                )

        if with_kafka and not self.with_kafka:
            cmds.append(
                self.setup_kafka_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_kafka_sasl and not self.with_kafka_sasl:
            cmds.append(
                self.setup_kafka_sasl_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_kerberized_kafka and not self.with_kerberized_kafka:
            cmds.append(
                self.setup_kerberized_kafka_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_kerberos_kdc and not self.with_kerberos_kdc:
            cmds.append(
                self.setup_kerberos_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_rabbitmq and not self.with_rabbitmq:
            cmds.append(
                self.setup_rabbitmq_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_nats and not self.with_nats:
            cmds.append(
                self.setup_nats_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_nginx and not self.with_nginx:
            cmds.append(
                self.setup_nginx_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_mongo and not self.with_mongo:
            cmds.append(
                self.setup_mongo_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_coredns and not self.with_coredns:
            cmds.append(
                self.setup_coredns_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_redis and not self.with_redis:
            cmds.append(
                self.setup_redis_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_minio and not self.with_minio:
            cmds.append(
                self.setup_minio_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_iceberg_catalog and not self.with_iceberg_catalog:
            cmds.append(
                self.setup_iceberg_catalog_cmd(
                    instance, env_variables, docker_compose_yml_dir, extra_parameters
                )
            )

        if with_glue_catalog and not self.with_glue_catalog:
            cmds.append(
                self.setup_glue_catalog_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_hms_catalog and not self.with_hms_catalog:
            cmds.append(
                self.setup_hms_catalog_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_azurite and not self.with_azurite:
            cmds.append(
                self.setup_azurite_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if minio_certs_dir is not None:
            if self.minio_certs_dir is None:
                self.minio_certs_dir = minio_certs_dir
            else:
                raise Exception("Overwriting minio certs dir")

        if minio_data_dir is not None:
            if self.minio_data_dir is None:
                self.minio_data_dir = minio_data_dir
            else:
                raise Exception("Overwriting minio data dir")

        if with_cassandra and not self.with_cassandra:
            cmds.append(
                self.setup_cassandra_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_ldap and not self.with_ldap:
            cmds.append(
                self.setup_ldap_cmd(instance, env_variables, docker_compose_yml_dir)
            )

        if with_jdbc_bridge and not self.with_jdbc_bridge:
            cmds.append(
                self.setup_jdbc_bridge_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        if with_hive:
            cmds.append(
                self.setup_hive(instance, env_variables, docker_compose_yml_dir)
            )

        if with_prometheus:
            if handle_prometheus_remote_write:
                self.prometheus_remote_write_handler_host = instance.hostname
            if handle_prometheus_remote_read:
                self.prometheus_remote_read_handler_host = instance.hostname
            cmds.append(
                self.setup_prometheus_cmd(
                    instance, env_variables, docker_compose_yml_dir
                )
            )

        ### !!!! This is the last step after combining all cmds, don't put anything after
        if self.with_net_trics:
            for cmd in cmds:
                # Again, adding it only once
                if docker_compose_net not in cmd:
                    cmd.extend(["--file", docker_compose_net])

        logging.debug(
            "Cluster name:{} project_name:{}. Added instance name:{} tag:{} base_cmd:{} docker_compose_yml_dir:{}".format(
                self.name,
                self.project_name,
                name,
                tag,
                self.base_cmd,
                docker_compose_yml_dir,
            )
        )
        return instance

    def get_instance_docker_id(self, instance_name):
        # According to how docker-compose names containers.
        return self.project_name + "-" + instance_name + "-1"

    def _replace(self, path, what, to):
        with open(path, "r") as p:
            data = p.read()
        data = data.replace(what, to)
        with open(path, "w") as p:
            p.write(data)

    def restart_instance_with_ip_change(self, node, new_ip):
        if "::" in new_ip:
            if node.ipv6_address is None:
                raise Exception("You should specify ipv6_address in add_node method")
            self._replace(node.docker_compose_path, node.ipv6_address, new_ip)
            node.ipv6_address = new_ip
        else:
            if node.ipv4_address is None:
                raise Exception("You should specify ipv4_address in add_node method")
            self._replace(node.docker_compose_path, node.ipv4_address, new_ip)
            node.ipv4_address = new_ip
        run_and_check(self.base_cmd + ["stop", node.name])
        run_and_check(self.base_cmd + ["rm", "--force", "--stop", node.name])
        run_and_check(
            self.base_cmd + ["up", "--force-recreate", "--no-deps", "-d", node.name]
        )
        node.ip_address = self.get_instance_ip(node.name)
        node.ipv6_address = self.get_instance_global_ipv6(node.name)
        node.client = Client(node.ip_address, command=self.client_bin_path)

        logging.info("Restart node with ip change")
        # In builds with sanitizer the server can take a long time to start
        node.wait_for_start(start_timeout=180.0, connection_timeout=600.0)  # seconds
        res = node.client.query("SELECT 30")
        logging.debug(f"Read '{res}'")
        assert "30\n" == res
        logging.info("Restarted")

        return node

    def restart_service(self, service_name):
        run_and_check(self.base_cmd + ["restart", service_name])

    def get_instance_ip(self, instance_name):
        logging.debug("get_instance_ip instance_name={}".format(instance_name))
        docker_id = self.get_instance_docker_id(instance_name)
        # for cont in self.docker_client.containers.list():
        # logging.debug("CONTAINERS LIST: ID={} NAME={} STATUS={}".format(cont.id, cont.name, cont.status))
        handle = self.docker_client.containers.get(docker_id)
        return list(handle.attrs["NetworkSettings"]["Networks"].values())[0][
            "IPAddress"
        ]

    def get_instance_global_ipv6(self, instance_name):
        logging.debug("get_instance_ip instance_name={}".format(instance_name))
        docker_id = self.get_instance_docker_id(instance_name)
        # for cont in self.docker_client.containers.list():
        # logging.debug("CONTAINERS LIST: ID={} NAME={} STATUS={}".format(cont.id, cont.name, cont.status))
        handle = self.docker_client.containers.get(docker_id)
        return list(handle.attrs["NetworkSettings"]["Networks"].values())[0][
            "GlobalIPv6Address"
        ]

    def get_container_id(self, instance_name):
        return self.get_instance_docker_id(instance_name)
        # docker_id = self.get_instance_docker_id(instance_name)
        # handle = self.docker_client.containers.get(docker_id)
        # return handle.attrs['Id']

    def get_container_logs(self, instance_name):
        container_id = self.get_container_id(instance_name)
        return self.docker_client.api.logs(container_id).decode()

    def query_zookeeper(self, query, node=ZOOKEEPER_CONTAINERS[0], nothrow=False):
        cmd = f'clickhouse keeper-client -p {self.zookeeper_port} -q "{query}"'
        container_id = self.get_container_id(node)
        return self.exec_in_container(container_id, cmd, nothrow=nothrow, use_cli=False)

    def exec_in_container(
        self,
        container_id: str,
        cmd: Sequence[str],
        detach: bool = False,
        nothrow: bool = False,
        use_cli: bool = True,
        get_exec_id: bool = False,
        **kwargs: Any,
    ) -> str:
        if use_cli:
            assert not get_exec_id
            logging.debug(
                f"run container_id:{container_id} detach:{detach} nothrow:{nothrow} cmd: {cmd}"
            )
            exec_cmd = ["docker", "exec"]
            if "user" in kwargs:
                exec_cmd += ["-u", kwargs["user"]]
            if "privileged" in kwargs:
                exec_cmd += ["--privileged"]

            env = None
            if "environment" in kwargs:
                env = kwargs.pop("environment", None)
                for k, v in env.items():
                    exec_cmd += ["--env", k + "=" + v]

            exec_cmd += [container_id]
            exec_cmd += list(cmd)

            result = subprocess_check_call(
                exec_cmd, detach=detach, nothrow=nothrow, env=env
            )
            return result
        else:
            assert self.docker_client is not None
            exec_id = self.docker_client.api.exec_create(container_id, cmd, **kwargs)
            output = self.docker_client.api.exec_start(exec_id, detach=detach)

            exit_code = self.docker_client.api.exec_inspect(exec_id)["ExitCode"]
            if exit_code:
                container_info = self.docker_client.api.inspect_container(container_id)
                image_id = container_info.get("Image")
                image_info = self.docker_client.api.inspect_image(image_id)
                logging.debug("Command failed in container %s: ", container_id)
                pprint.pprint(container_info)
                logging.debug("")
                logging.debug("Container %s uses image %s: ", container_id, image_id)
                pprint.pprint(image_info)
                logging.debug("")
                message = (
                    f'Cmd "{" ".join(cmd)}" failed in container {container_id}. '
                    f"Return code {exit_code}. Output: {output}"
                )
                if nothrow:
                    logging.debug(message)
                else:
                    raise Exception(message)
            if not detach:
                assert not get_exec_id
                return output.decode()
            return exec_id if get_exec_id else output

    def copy_file_to_container(self, container_id, local_path, dest_path):
        with open(local_path, "rb") as fdata:
            data = fdata.read()
            encodedBytes = base64.b64encode(data)
            encodedStr = str(encodedBytes, "utf-8")
            self.exec_in_container(
                container_id,
                [
                    "bash",
                    "-c",
                    "mkdir -p $(dirname {}) && echo {} | base64 --decode > {}".format(
                        dest_path, encodedStr, dest_path
                    ),
                ],
            )

    def copy_file_from_container(self, container_id, src_path, local_path):
        result = self.exec_in_container(
            container_id,
            [
                "bash",
                "-c",
                "base64 {}".format(src_path),
            ],
        )

        if result:
            decoded_data = base64.b64decode(result)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(decoded_data)
        else:
            raise RuntimeError(f"Failed to read or empty content from {src_path} in container {container_id}")

    def file_exists_in_container(self, container_id, path):
        try:
            self.exec_in_container(
                container_id,
                ["bash", "-c", f"test -f {path}"]
            )
            return True
        except Exception:
            return False
        
    def get_files_list_in_container(self, container_id, path):
        result = self.exec_in_container(
            container_id,
            [
                "bash",
                "-c",
                f"find {path} -type f"
            ],
        )

        files = result.strip().splitlines() if result else []
        return files            

    def move_file_in_container(self, container_id, old_path, new_path):
        self.exec_in_container(
            container_id,
            [
                "bash",
                "-c",
                "mv {} {}".format(old_path, new_path),
            ],
        )

    def remove_file_from_container(self, container_id, path):
        self.exec_in_container(
            container_id,
            [
                "bash",
                "-c",
                "rm {}".format(path),
            ],
        )

    def wait_for_url(
        self, url="http://localhost:8123/ping", conn_timeout=2, interval=2, timeout=60
    ):
        if not url.startswith("http"):
            url = "http://" + url
        if interval <= 0:
            interval = 2
        if timeout <= 0:
            timeout = 60

        attempts = 1
        errors = []
        start = time.time()
        while time.time() - start < timeout:
            try:
                requests.get(
                    url, allow_redirects=True, timeout=conn_timeout, verify=False
                ).raise_for_status()
                logging.debug(
                    "{} is available after {} seconds".format(url, time.time() - start)
                )
                return
            except Exception as ex:
                logging.debug(
                    "{} Attempt {} failed, retrying in {} seconds".format(
                        ex, attempts, interval
                    )
                )
                attempts += 1
                errors += [str(ex)]
                time.sleep(interval)

        run_and_check(["docker", "ps", "--all"])
        logging.error("Can't connect to URL:{}".format(errors))
        raise Exception(
            "Cannot wait URL {}(interval={}, timeout={}, attempts={})".format(
                url, interval, timeout, attempts
            )
        )

    def wait_mysql_client_to_start(self, timeout=180):
        start = time.time()
        errors = []
        self.mysql_client_container = self.get_docker_handle(
            self.get_instance_docker_id(self.mysql_client_host)
        )

        while time.time() - start < timeout:
            try:
                info = self.mysql_client_container.client.api.inspect_container(
                    self.mysql_client_container.name
                )
                if info["State"]["Health"]["Status"] == "healthy":
                    logging.debug("Mysql Client Container Started")
                    return
                time.sleep(1)
            except Exception as ex:
                errors += [str(ex)]
                time.sleep(1)

        run_and_check(["docker", "ps", "--all"])
        logging.error("Can't connect to MySQL Client:{}".format(errors))
        raise Exception("Cannot wait MySQL Client container")

    def wait_mysql57_to_start(self, timeout=180):
        self.mysql57_ip = self.get_instance_ip("mysql57")
        start = time.time()
        errors = []
        while time.time() - start < timeout:
            try:
                conn = pymysql.connect(
                    user=mysql_user,
                    password=mysql_pass,
                    host=self.mysql57_ip,
                    port=self.mysql57_port,
                )
                conn.close()
                logging.debug("Mysql Started")
                return
            except Exception as ex:
                errors += [str(ex)]
                time.sleep(0.5)

        run_and_check(["docker", "ps", "--all"])
        logging.error("Can't connect to MySQL:{}".format(errors))
        raise Exception("Cannot wait MySQL container")

    def wait_mysql8_to_start(self, timeout=180):
        self.mysql8_ip = self.get_instance_ip("mysql80")
        start = time.time()
        while time.time() - start < timeout:
            try:
                conn = pymysql.connect(
                    user=mysql_user,
                    password=mysql_pass,
                    host=self.mysql8_ip,
                    port=self.mysql8_port,
                )
                conn.close()
                logging.debug("Mysql 8 Started")
                return
            except Exception as ex:
                logging.debug("Can't connect to MySQL 8 " + str(ex))
                time.sleep(0.5)

        run_and_check(["docker", "ps", "--all"])
        raise Exception("Cannot wait MySQL 8 container")

    def wait_mysql_cluster_to_start(self, timeout=180):
        self.mysql2_ip = self.get_instance_ip(self.mysql2_host)
        self.mysql3_ip = self.get_instance_ip(self.mysql3_host)
        self.mysql4_ip = self.get_instance_ip(self.mysql4_host)
        start = time.time()
        errors = []
        while time.time() - start < timeout:
            try:
                for ip in [self.mysql2_ip, self.mysql3_ip, self.mysql4_ip]:
                    conn = pymysql.connect(
                        user=mysql_user,
                        password=mysql_pass,
                        host=ip,
                        port=self.mysql8_port,
                    )
                    conn.close()
                    logging.debug(f"Mysql Started {ip}")
                return
            except Exception as ex:
                errors += [str(ex)]
                time.sleep(0.5)

        run_and_check(["docker", "ps", "--all"])
        logging.error("Can't connect to MySQL:{}".format(errors))
        raise Exception("Cannot wait MySQL container")

    def wait_postgres_to_start(self, timeout=260):
        self.postgres_ip = self.get_instance_ip(self.postgres_host)
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.postgres_conn = psycopg2.connect(
                    host=self.postgres_ip,
                    port=self.postgres_port,
                    database=pg_db,
                    user=pg_user,
                    password=pg_pass,
                )
                self.postgres_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                self.postgres_conn.autocommit = True
                logging.debug("Postgres Started")
                return
            except Exception as ex:
                logging.debug("Can't connect to Postgres " + str(ex))
                time.sleep(0.5)

        raise Exception("Cannot wait Postgres container")

    def wait_postgres_cluster_to_start(self, timeout=180):
        self.postgres2_ip = self.get_instance_ip(self.postgres2_host)
        self.postgres3_ip = self.get_instance_ip(self.postgres3_host)
        self.postgres4_ip = self.get_instance_ip(self.postgres4_host)
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.postgres2_conn = psycopg2.connect(
                    host=self.postgres2_ip,
                    port=self.postgres_port,
                    database=pg_db,
                    user=pg_user,
                    password=pg_pass,
                )
                self.postgres2_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                self.postgres2_conn.autocommit = True
                logging.debug("Postgres Cluster host 2 started")
                break
            except Exception as ex:
                logging.debug("Can't connect to Postgres host 2" + str(ex))
                time.sleep(0.5)
        while time.time() - start < timeout:
            try:
                self.postgres3_conn = psycopg2.connect(
                    host=self.postgres3_ip,
                    port=self.postgres_port,
                    database=pg_db,
                    user=pg_user,
                    password=pg_pass,
                )
                self.postgres3_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                self.postgres3_conn.autocommit = True
                logging.debug("Postgres Cluster host 3 started")
                break
            except Exception as ex:
                logging.debug("Can't connect to Postgres host 3" + str(ex))
                time.sleep(0.5)
        while time.time() - start < timeout:
            try:
                self.postgres4_conn = psycopg2.connect(
                    host=self.postgres4_ip,
                    port=self.postgres_port,
                    database=pg_db,
                    user=pg_user,
                    password=pg_pass,
                )
                self.postgres4_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                self.postgres4_conn.autocommit = True
                logging.debug("Postgres Cluster host 4 started")
                return
            except Exception as ex:
                logging.debug("Can't connect to Postgres host 4" + str(ex))
                time.sleep(0.5)

        raise Exception("Cannot wait Postgres container")

    def wait_postgresql_java_client(self, timeout=180):
        start = time.time()
        while time.time() - start < timeout:
            try:
                if check_postgresql_java_client_is_available(
                    self.postgresql_java_client_docker_id
                ):
                    logging.debug("PostgreSQL Java Client is available")
                    return True
                time.sleep(0.5)
            except Exception as ex:
                logging.debug("Can't find PostgreSQL Java Client" + str(ex))
                time.sleep(0.5)
        raise Exception("Cannot wait PostgreSQL Java Client container")

    def wait_rabbitmq_to_start(self, timeout=120):
        self.print_all_docker_pieces()
        self.rabbitmq_ip = self.get_instance_ip(self.rabbitmq_host)

        start = time.time()
        while time.time() - start < timeout:
            try:
                if check_rabbitmq_is_available(
                    self.rabbitmq_docker_id, self.rabbitmq_cookie
                ):
                    logging.debug("RabbitMQ is available")
                    return True
            except Exception as ex:
                logging.debug("RabbitMQ await_startup failed, %s:", ex)
                time.sleep(1)

        start = time.time()
        while time.time() - start < timeout:
            try:
                with open(os.path.join(self.rabbitmq_dir, "docker.log"), "w+") as f:
                    subprocess.check_call(  # STYLE_CHECK_ALLOW_SUBPROCESS_CHECK_CALL
                        self.base_rabbitmq_cmd + ["logs"], stdout=f
                    )
                rabbitmq_debuginfo(self.rabbitmq_docker_id, self.rabbitmq_cookie)
            except Exception as ex:
                logging.debug("Unable to get logs from docker: %s:", ex)
                time.sleep(0.5)

        raise RuntimeError("Cannot wait RabbitMQ container")

    def stop_rabbitmq_app(self, timeout=120):
        run_rabbitmqctl(
            self.rabbitmq_docker_id, self.rabbitmq_cookie, "stop_app", timeout
        )

    def start_rabbitmq_app(self, timeout=120):
        run_rabbitmqctl(
            self.rabbitmq_docker_id, self.rabbitmq_cookie, "start_app", timeout
        )
        self.wait_rabbitmq_to_start(timeout)

    @contextmanager
    def pause_rabbitmq(self, monitor=None, timeout=120):
        if monitor is not None:
            monitor.stop()
        self.stop_rabbitmq_app(timeout)

        try:
            yield
        finally:
            self.start_rabbitmq_app(timeout)
            if monitor is not None:
                monitor.start(self)

    def reset_rabbitmq(self, timeout=240):
        self.stop_rabbitmq_app()
        run_rabbitmqctl(self.rabbitmq_docker_id, self.rabbitmq_cookie, "reset", timeout)
        self.start_rabbitmq_app()

    def run_rabbitmqctl(self, command):
        run_rabbitmqctl(self.rabbitmq_docker_id, self.rabbitmq_cookie, command)

    def wait_nats_is_available(self, max_retries=5):
        retries = 0
        while True:
            if asyncio.run(
                check_nats_is_available(self.nats_port, ssl_ctx=self.nats_ssl_context)
            ):
                break
            else:
                retries += 1
                if retries > max_retries:
                    raise Exception("NATS is not available")
                logging.debug("Waiting for NATS to start up")
                time.sleep(1)

    def wait_zookeeper_secure_to_start(self, timeout=20):
        logging.debug("Wait ZooKeeper Secure to start")
        self.wait_zookeeper_nodes_to_start(ZOOKEEPER_CONTAINERS, timeout)

    def wait_zookeeper_to_start(self, timeout: float = 180) -> None:
        logging.debug("Wait ZooKeeper to start")
        self.wait_zookeeper_nodes_to_start(ZOOKEEPER_CONTAINERS, timeout)

    def wait_zookeeper_nodes_to_start(
        self,
        nodes: List[str],
        timeout: float = 60,
    ) -> None:
        start = time.time()
        err = Exception("")
        while time.time() - start < timeout:
            try:
                for node in nodes:
                    conn = self.get_kazoo_client(node)
                    conn.get_children("/")
                    conn.stop()
                logging.debug("All instances of ZooKeeper started: %s", nodes)
                return
            except Exception as ex:
                logging.debug("Can't connect to ZooKeeper %s: %s", node, ex)
                err = ex
                time.sleep(0.5)

        raise Exception(
            "Cannot wait ZooKeeper container (probably it's a `iptables-nft` issue, you may try to `sudo iptables -P FORWARD ACCEPT`)"
        ) from err

    def wait_kafka_is_available(self, kafka_docker_id, kafka_port, max_retries=50):
        retries = 0
        while True:
            if check_kafka_is_available(kafka_docker_id, kafka_port):
                return
            else:
                retries += 1
                if retries > max_retries:
                    break
                logging.debug("Waiting for Kafka to start up")
                time.sleep(1)

        try:
            with open(os.path.join(self.kafka_dir, "docker.log"), "w+") as f:
                subprocess.check_call(  # STYLE_CHECK_ALLOW_SUBPROCESS_CHECK_CALL
                    self.base_kafka_cmd + ["logs"], stdout=f
                )
        except Exception as e:
            logging.debug("Unable to get logs from docker.")
        raise Exception("Kafka is not available")

    def wait_kerberos_kdc_is_available(self, kerberos_kdc_docker_id, max_retries=50):
        retries = 0
        while True:
            if check_kerberos_kdc_is_available(kerberos_kdc_docker_id):
                break
            else:
                retries += 1
                if retries > max_retries:
                    raise Exception("Kerberos KDC is not available")
                logging.debug("Waiting for Kerberos KDC to start up")
                time.sleep(1)

    def wait_mongo_to_start(self, timeout=30, secure=False):
        connection_str = "mongodb://{user}:{password}@{host}:{port}".format(
            host="localhost",
            port=self.mongo_port,
            user=mongo_user,
            password=urllib.parse.quote_plus(mongo_pass),
        )
        if secure:
            connection_str += "/?tls=true&tlsAllowInvalidCertificates=true"
        connection = pymongo.MongoClient(connection_str)
        start = time.time()
        while time.time() - start < timeout:
            try:
                connection.list_database_names()
                logging.debug(
                    f"Connected to Mongo dbs: {connection.list_database_names()}"
                )
                return
            except Exception as ex:
                logging.debug("Can't connect to Mongo " + str(ex))
                time.sleep(1)

    def wait_custom_minio_to_start(self, buckets, host, port, timeout=180):
        ip = self.get_instance_ip(host)
        minio_client = Minio(
            f"{ip}:{port}",
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            secure=False,
            http_client=urllib3.PoolManager(cert_reqs="CERT_NONE"),
        )
        start = time.time()
        while time.time() - start < timeout:
            try:
                minio_client.list_buckets()

                logging.debug("Connected to Minio.")

                if all(minio_client.bucket_exists(bucket) for bucket in buckets):
                    return

                time.sleep(1)
            except Exception as ex:
                logging.debug("Can't connect to Minio: %s", str(ex))
                time.sleep(1)

        raise Exception("Can't wait Minio to start")

    def wait_minio_to_start(self, timeout=180, secure=False):
        self.minio_ip = self.get_instance_ip(self.minio_host)
        self.minio_redirect_ip = self.get_instance_ip(self.minio_redirect_host)

        os.environ["SSL_CERT_FILE"] = p.join(
            self.base_dir, self.minio_dir, "certs", "public.crt"
        )
        minio_client = Minio(
            f"{self.minio_ip}:{self.minio_port}",
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            secure=secure,
            http_client=urllib3.PoolManager(cert_reqs="CERT_NONE"),
        )  # disable SSL check as we test ClickHouse and not Python library
        start = time.time()
        while time.time() - start < timeout:
            try:
                minio_client.list_buckets()

                logging.debug("Connected to Minio.")

                buckets = [
                    self.minio_bucket,
                    self.minio_bucket_2,
                    self.minio_bucket_db_disk,
                ]

                for bucket in buckets:
                    if minio_client.bucket_exists(bucket):
                        delete_object_list = map(
                            lambda x: x.object_name,
                            minio_client.list_objects_v2(bucket, recursive=True),
                        )
                        errors = minio_client.remove_objects(bucket, delete_object_list)
                        for error in errors:
                            logging.error(
                                f"Error occurred when deleting object {error}"
                            )
                        minio_client.remove_bucket(bucket)
                    minio_client.make_bucket(bucket)
                    logging.debug("S3 bucket '%s' created", bucket)

                self.minio_client = minio_client
                return
            except Exception as ex:
                logging.debug("Can't connect to Minio: %s", str(ex))
                time.sleep(1)

        try:
            with open(os.path.join(self.minio_dir, "docker.log"), "w+") as f:
                subprocess.check_call(  # STYLE_CHECK_ALLOW_SUBPROCESS_CHECK_CALL
                    self.base_minio_cmd + ["logs"], stdout=f
                )
        except Exception as e:
            logging.debug("Unable to get logs from docker.")

        raise Exception("Can't wait Minio to start")

    def wait_azurite_to_start(self, timeout=180):
        from azure.storage.blob import BlobServiceClient

        connection_string = (
            f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            f"AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
            f"BlobEndpoint=http://127.0.0.1:{self.env_variables['AZURITE_PORT']}/devstoreaccount1;"
        )
        time.sleep(1)
        start = time.time()
        while time.time() - start < timeout:
            try:
                blob_service_client = BlobServiceClient.from_connection_string(
                    connection_string
                )
                logging.debug(blob_service_client.get_account_information())
                containers = [
                    c
                    for c in blob_service_client.list_containers(
                        name_starts_with=self.azurite_container
                    )
                    if c.name == self.azurite_container
                ]
                if len(containers) > 0:
                    for c in containers:
                        blob_service_client.delete_container(c)

                container_client = blob_service_client.get_container_client(
                    self.azurite_container
                )
                if container_client.exists():
                    logging.debug(
                        f"azurite container '{self.azurite_container}' exist, deleting all blobs"
                    )
                    for b in container_client.list_blobs():
                        container_client.delete_blob(b.name)
                else:
                    logging.debug(
                        f"azurite container '{self.azurite_container}' doesn't exist, creating it"
                    )
                    container_client.create_container()

                self.blob_service_client = blob_service_client
                return
            except Exception as ex:
                logging.debug("Can't connect to Azurite: %s", str(ex))
                time.sleep(1)

        raise Exception("Can't wait Azurite to start")

    def wait_schema_registry_to_start(self, timeout=180):
        for port in self.schema_registry_port, self.schema_registry_auth_port:
            reg_url = "http://localhost:{}".format(port)
            arg = {"url": reg_url}
            sr_client = CachedSchemaRegistryClient(arg)

            start = time.time()
            sr_started = False
            sr_auth_started = False
            while time.time() - start < timeout:
                try:
                    sr_client._send_request(sr_client.url)
                    logging.debug("Connected to SchemaRegistry")
                    # don't care about possible auth errors
                    sr_started = True
                    break
                except Exception as ex:
                    logging.debug(("Can't connect to SchemaRegistry: %s", str(ex)))
                    time.sleep(1)

            if not sr_started:
                raise Exception("Can't wait Schema Registry to start")

    def wait_cassandra_to_start(self, timeout=180):
        self.cassandra_ip = self.get_instance_ip(self.cassandra_host)
        cass_client = cassandra.cluster.Cluster(
            [self.cassandra_ip],
            port=self.cassandra_port,
            load_balancing_policy=RoundRobinPolicy(),
        )
        start = time.time()
        while time.time() - start < timeout:
            try:
                logging.info(
                    f"Check Cassandra Online {self.cassandra_id} {self.cassandra_ip} {self.cassandra_port}"
                )
                self.exec_in_container(
                    self.cassandra_id,
                    [
                        "bash",
                        "-c",
                        f"/opt/cassandra/bin/cqlsh -u cassandra -p cassandra -e 'describe keyspaces' {self.cassandra_ip} {self.cassandra_port}",
                    ],
                    user="root",
                )
                logging.info("Cassandra Online")
                cass_client.connect()
                logging.info("Connected Clients to Cassandra")
                return
            except Exception as ex:
                logging.warning("Can't connect to Cassandra: %s", str(ex))
                time.sleep(1)

        raise Exception("Can't wait Cassandra to start")

    def wait_ldap_to_start(self, timeout=180):
        self.ldap_container = self.get_docker_handle(self.ldap_id)
        start = time.time()
        while time.time() - start < timeout:
            try:
                logging.info(f"Check LDAP Online {self.ldap_host} {self.ldap_port}")
                self.exec_in_container(
                    self.ldap_id,
                    [
                        "bash",
                        "-c",
                        "test -f /tmp/.openldap-initialized"
                        f"&& /opt/bitnami/openldap/bin/ldapsearch -x -H ldap://{self.ldap_host}:{self.ldap_port} -D cn=admin,dc=example,dc=org -w clickhouse -b dc=example,dc=org"
                        f'| grep -c -E "member: cn=j(ohn|ane)doe"'
                        f"| grep 2 >> /dev/null",
                    ],
                    user="root",
                )
                logging.info("LDAP Online")
                return
            except Exception as ex:
                logging.warning("Can't connect to LDAP: %s", str(ex))
                time.sleep(1)

        raise Exception("Can't wait LDAP to start")

    def wait_prometheus_to_start(self):
        self.prometheus_reader_ip = self.get_instance_ip(self.prometheus_reader_host)
        self.prometheus_writer_ip = self.get_instance_ip(self.prometheus_writer_host)
        self.wait_for_url(
            f"http://{self.prometheus_reader_ip}:{self.prometheus_reader_port}/api/v1/query?query=time()"
        )
        self.wait_for_url(
            f"http://{self.prometheus_writer_ip}:{self.prometheus_writer_port}/api/v1/query?query=time()"
        )

    def start(self):
        pytest_xdist_logging_to_separate_files.setup()
        logging.info("Running tests in {}".format(self.base_path))
        if not os.path.exists(self.instances_dir):
            os.mkdir(self.instances_dir)
        else:
            logging.warning(
                "Instance directory already exists. Did you call cluster.start() for second time?"
            )
        logging.debug(f"Cluster start called. is_up={self.is_up}")
        self.print_all_docker_pieces()

        if self.is_up:
            return

        try:
            self.cleanup()
        except Exception as e:
            logging.warning("Cleanup failed:{e}")

        try:
            for instance in list(self.instances.values()):
                logging.debug(f"Setup directory for instance: {instance.name}")
                instance.create_dir()

            _create_env_file(os.path.join(self.env_file), self.env_variables)
            self.docker_client = docker.DockerClient(
                base_url="unix:///var/run/docker.sock",
                version=self.docker_api_version,
                timeout=600,
            )

            common_opts = ["--verbose", "up", "-d"]

            images_pull_cmd = self.base_cmd + ["pull"]
            # sometimes dockerhub/proxy can be flaky

            def logging_pulling_images(**kwargs):
                if "exception" in kwargs:
                    logging.info(
                        "Got exception pulling images: %s", kwargs["exception"]
                    )

            retry(log_function=logging_pulling_images)(run_and_check, images_pull_cmd)

            if self.with_zookeeper_secure and self.base_zookeeper_cmd:
                logging.debug("Setup ZooKeeper Secure")
                logging.debug(
                    f"Creating internal ZooKeeper dirs: {self.zookeeper_dirs_to_create}"
                )
                for i in range(1, 3):
                    if os.path.exists(self.zookeeper_instance_dir_prefix + f"{i}"):
                        shutil.rmtree(self.zookeeper_instance_dir_prefix + f"{i}")
                for dir in self.zookeeper_dirs_to_create:
                    os.makedirs(dir)
                run_and_check(self.base_zookeeper_cmd + common_opts, env=self.env)
                self.up_called = True

                self.wait_zookeeper_secure_to_start()
                for command in self.pre_zookeeper_commands:
                    self.run_kazoo_commands_with_retries(command, repeats=5)

            if self.with_zookeeper and self.base_zookeeper_cmd:
                logging.debug("Setup ZooKeeper")
                logging.debug(
                    f"Creating internal ZooKeeper dirs: {self.zookeeper_dirs_to_create}"
                )
                if self.use_keeper:
                    for i in range(1, 4):
                        if os.path.exists(self.keeper_instance_dir_prefix + f"{i}"):
                            shutil.rmtree(self.keeper_instance_dir_prefix + f"{i}")
                else:
                    for i in range(1, 3):
                        if os.path.exists(self.zookeeper_instance_dir_prefix + f"{i}"):
                            shutil.rmtree(self.zookeeper_instance_dir_prefix + f"{i}")

                for dir in self.zookeeper_dirs_to_create:
                    os.makedirs(dir)

                if self.use_keeper:  # TODO: remove hardcoded paths from here
                    for i in range(1, 4):
                        current_keeper_config_dir = os.path.join(
                            f"{self.keeper_instance_dir_prefix}{i}", "config"
                        )
                        if self.custom_keeper_configs_paths is None:
                            shutil.copy(
                                os.path.join(
                                    self.keeper_config_dir, f"keeper_config{i}.xml"
                                ),
                                current_keeper_config_dir,
                            )

                            extra_configs_dir = os.path.join(
                                current_keeper_config_dir, f"keeper_config{i}.d"
                            )
                            os.mkdir(extra_configs_dir)
                            feature_flags_config = os.path.join(
                                extra_configs_dir, "feature_flags.yaml"
                            )

                            indentation = 4 * " "

                            def get_feature_flag_value(feature_flag):
                                if not self.keeper_randomize_feature_flags:
                                    return 1

                                if feature_flag in self.keeper_required_feature_flags:
                                    return 1

                                return random.randint(0, 1)

                            with open(feature_flags_config, "w") as ff_config:
                                ff_config.write("keeper_server:\n")
                                ff_config.write(f"{indentation}feature_flags:\n")
                                indentation *= 2

                                for feature_flag in [
                                    "filtered_list",
                                    "multi_read",
                                    "check_not_exists",
                                    "create_if_not_exists",
                                    "remove_recursive",
                                ]:
                                    ff_config.write(
                                        f"{indentation}{feature_flag}: {get_feature_flag_value(feature_flag)}\n"
                                    )
                        else:
                            basename = os.path.basename(
                                self.custom_keeper_configs_paths[i - 1]
                            )
                            shutil.copy(
                                self.custom_keeper_configs_paths[i - 1],
                                current_keeper_config_dir,
                            )
                            os.rename(
                                os.path.join(current_keeper_config_dir, basename),
                                os.path.join(
                                    current_keeper_config_dir, f"keeper_config{i}.xml"
                                ),
                            )

                run_and_check(self.base_zookeeper_cmd + common_opts, env=self.env)
                self.up_called = True

                self.wait_zookeeper_to_start()
                for command in self.pre_zookeeper_commands:
                    self.run_kazoo_commands_with_retries(command, repeats=5)

            for instance in list(self.instances.values()):
                if instance.with_remote_database_disk:
                    logging.debug(
                        f"Setup with_remote_database_disk, instance {instance.name}"
                    )
                    config_file_path = os.path.join(
                        HELPERS_DIR, "remote_database_disk.xml"
                    )
                    with open(config_file_path, "r") as config_source_file:
                        data = config_source_file.read()
                    data = data.format(
                        host=self.minio_host,
                        port=str(self.minio_port),
                        bucket=self.minio_bucket_db_disk,
                        shard="{shard}",
                        replica="{replica}",
                    )
                    instance_config_dir = os.path.join(instance.path, "configs")
                    target_config_file_path = os.path.join(
                        instance_config_dir,
                        "config.d",
                        "remote_database_disk.xml",
                    )
                    with open(target_config_file_path, "w") as config_target_file:
                        config_target_file.write(data)

            if self.with_mysql_client and self.base_mysql_client_cmd:
                logging.debug("Setup MySQL Client")
                subprocess_check_call(self.base_mysql_client_cmd + common_opts)
                self.wait_mysql_client_to_start()

            if self.with_mysql57 and self.base_mysql57_cmd:
                logging.debug("Setup MySQL")
                if os.path.exists(self.mysql57_dir):
                    shutil.rmtree(self.mysql57_dir)
                os.makedirs(self.mysql57_logs_dir)
                os.chmod(self.mysql57_logs_dir, stat.S_IRWXU | stat.S_IRWXO)
                subprocess_check_call(self.base_mysql57_cmd + common_opts)
                self.up_called = True
                self.wait_mysql57_to_start()

            if self.with_mysql8 and self.base_mysql8_cmd:
                logging.debug("Setup MySQL 8")
                if os.path.exists(self.mysql8_dir):
                    shutil.rmtree(self.mysql8_dir)
                os.makedirs(self.mysql8_logs_dir)
                os.chmod(self.mysql8_logs_dir, stat.S_IRWXU | stat.S_IRWXO)
                subprocess_check_call(self.base_mysql8_cmd + common_opts)
                self.wait_mysql8_to_start()

            if self.with_mysql_cluster and self.base_mysql_cluster_cmd:
                print("Setup MySQL")
                if os.path.exists(self.mysql_cluster_dir):
                    shutil.rmtree(self.mysql_cluster_dir)
                os.makedirs(self.mysql_cluster_logs_dir, exist_ok=True)
                os.chmod(self.mysql_cluster_logs_dir, stat.S_IRWXU | stat.S_IRWXO)

                subprocess_check_call(self.base_mysql_cluster_cmd + common_opts)
                self.up_called = True
                self.wait_mysql_cluster_to_start()

            if self.with_postgres and self.base_postgres_cmd:
                logging.debug("Setup Postgres")
                if os.path.exists(self.postgres_dir):
                    shutil.rmtree(self.postgres_dir)
                os.makedirs(self.postgres_logs_dir)
                os.chmod(self.postgres_logs_dir, stat.S_IRWXU | stat.S_IRWXO)

                subprocess_check_call(self.base_postgres_cmd + common_opts)
                self.up_called = True
                self.wait_postgres_to_start()

            if self.with_postgres_cluster and self.base_postgres_cluster_cmd:
                logging.debug("Setup Postgres")
                os.makedirs(self.postgres2_logs_dir)
                os.chmod(self.postgres2_logs_dir, stat.S_IRWXU | stat.S_IRWXO)
                os.makedirs(self.postgres3_logs_dir)
                os.chmod(self.postgres3_logs_dir, stat.S_IRWXU | stat.S_IRWXO)
                os.makedirs(self.postgres4_logs_dir)
                os.chmod(self.postgres4_logs_dir, stat.S_IRWXU | stat.S_IRWXO)
                subprocess_check_call(self.base_postgres_cluster_cmd + common_opts)
                self.up_called = True
                self.wait_postgres_cluster_to_start()

            if (
                self.with_postgresql_java_client
                and self.base_postgresql_java_client_cmd
            ):
                logging.debug("Setup Postgres Java Client")
                subprocess_check_call(
                    self.base_postgresql_java_client_cmd + common_opts
                )
                self.up_called = True
                self.wait_postgresql_java_client()

            if self.with_kafka and self.base_kafka_cmd:
                logging.debug("Setup Kafka")
                os.mkdir(self.kafka_dir)
                subprocess_check_call(
                    self.base_kafka_cmd + common_opts + ["--renew-anon-volumes"]
                )
                self.up_called = True
                self.wait_kafka_is_available(self.kafka_docker_id, self.kafka_port)
                self.wait_schema_registry_to_start()

            if self.with_kafka_sasl and self.base_kafka_sasl_cmd:
                logging.debug("Setup Kafka with SASL")
                os.mkdir(self.kafka_sasl_dir)
                subprocess_check_call(
                    self.base_kafka_sasl_cmd + common_opts + ["--renew-anon-volumes"]
                )
                self.up_called = True

            if self.with_kerberized_kafka and self.base_kerberized_kafka_cmd:
                logging.debug("Setup kerberized kafka")
                os.mkdir(self.kafka_dir)
                run_and_check(
                    self.base_kerberized_kafka_cmd
                    + common_opts
                    + ["--renew-anon-volumes"]
                )
                self.up_called = True
                self.wait_kafka_is_available(
                    self.kerberized_kafka_docker_id, self.kerberized_kafka_port, 100
                )

            if self.with_kerberos_kdc and self.base_kerberos_kdc_cmd:
                logging.debug("Setup Kerberos KDC")
                run_and_check(
                    self.base_kerberos_kdc_cmd + common_opts + ["--renew-anon-volumes"]
                )
                self.up_called = True
                self.wait_kerberos_kdc_is_available(self.keberos_kdc_docker_id)

            if self.with_rabbitmq and self.base_rabbitmq_cmd:
                logging.debug("Setup RabbitMQ")
                os.makedirs(self.rabbitmq_logs_dir)
                os.chmod(self.rabbitmq_logs_dir, stat.S_IRWXU | stat.S_IRWXO)

                with open(self.rabbitmq_cookie_file, "w") as f:
                    f.write(self.rabbitmq_cookie)
                os.chmod(self.rabbitmq_cookie_file, stat.S_IRUSR)

                subprocess_check_call(
                    self.base_rabbitmq_cmd + common_opts + ["--renew-anon-volumes"]
                )
                self.up_called = True
                self.rabbitmq_docker_id = self.get_instance_docker_id("rabbitmq1")
                time.sleep(2)
                logging.debug(f"RabbitMQ checking container try")
                self.wait_rabbitmq_to_start()

            if self.with_nats and self.base_nats_cmd:
                logging.debug("Setup NATS")
                os.makedirs(self.nats_cert_dir)
                env = os.environ.copy()
                env["NATS_CERT_DIR"] = self.nats_cert_dir
                run_and_check(
                    p.join(self.base_dir, "nats_certs.sh"),
                    env=env,
                    detach=False,
                    nothrow=False,
                )

                self.nats_ssl_context = ssl.create_default_context()
                self.nats_ssl_context.load_verify_locations(
                    p.join(self.nats_cert_dir, "ca", "ca-cert.pem")
                )
                subprocess_check_call(self.base_nats_cmd + common_opts)
                self.nats_docker_id = self.get_instance_docker_id("nats1")
                self.up_called = True
                self.wait_nats_is_available()

            if self.with_nginx and self.base_nginx_cmd:
                logging.debug("Setup nginx")
                subprocess_check_call(
                    self.base_nginx_cmd + common_opts + ["--renew-anon-volumes"]
                )
                self.up_called = True
                self.nginx_docker_id = self.get_instance_docker_id("nginx")

            if self.with_mongo and self.base_mongo_cmd:
                logging.debug("Setup Mongo")
                run_and_check(self.base_mongo_cmd + common_opts)
                self.up_called = True
                self.wait_mongo_to_start(30)

            if self.with_coredns and self.base_coredns_cmd:
                logging.debug("Setup coredns")
                run_and_check(self.base_coredns_cmd + common_opts)
                self.up_called = True
                time.sleep(10)

            if self.with_redis and self.base_redis_cmd:
                logging.debug("Setup Redis")
                subprocess_check_call(self.base_redis_cmd + common_opts)
                self.up_called = True
                time.sleep(10)

            if self.with_hive and self.base_hive_cmd:
                logging.debug("Setup hive")
                subprocess_check_call(self.base_hive_cmd + common_opts)
                self.up_called = True
                time.sleep(30)

            if self.with_minio and self.base_minio_cmd:
                # Copy minio certificates to minio/certs
                os.mkdir(self.minio_dir)
                if self.minio_certs_dir is None:
                    os.mkdir(os.path.join(self.minio_dir, "certs"))
                    os.mkdir(os.path.join(self.minio_dir, "certs", "CAs"))
                else:
                    shutil.copytree(
                        os.path.join(self.base_dir, self.minio_certs_dir),
                        os.path.join(self.minio_dir, "certs"),
                    )
                os.mkdir(self.minio_data_dir)
                os.chmod(self.minio_data_dir, stat.S_IRWXU | stat.S_IRWXO)

                os.makedirs(self.resolver_logs_dir)
                os.chmod(self.resolver_logs_dir, stat.S_IRWXU | stat.S_IRWXO)

                minio_start_cmd = self.base_minio_cmd + common_opts

                logging.info(
                    "Trying to create Minio instance by command %s",
                    " ".join(map(str, minio_start_cmd)),
                )
                run_and_check(minio_start_cmd)
                self.up_called = True
                logging.info("Trying to connect to Minio...")
                self.wait_minio_to_start(secure=self.minio_certs_dir is not None)

            if self.with_glue_catalog and self.base_glue_catalog_cmd:
                logging.info("Trying to connect to Minio for glue catalog...")
                subprocess_check_call(self.base_glue_catalog_cmd + common_opts)
                self.up_called = True
                self.wait_custom_minio_to_start(["warehouse-glue"], "minio", 9000)

            if self.with_hms_catalog and self.base_iceberg_hms_cmd:
                logging.info("Trying to connect to Minio for hms catalog...")
                subprocess_check_call(self.base_iceberg_hms_cmd + common_opts)
                self.up_called = True
                self.wait_custom_minio_to_start(["warehouse-hms"], "minio", 9000)

            if self.with_iceberg_catalog and self.base_iceberg_catalog_cmd:
                logging.info("Trying to connect to Minio for Iceberg catalog...")
                subprocess_check_call(self.base_iceberg_catalog_cmd + common_opts)
                self.up_called = True
                self.wait_custom_minio_to_start(["warehouse-rest"], "minio", 9000)

            if self.with_azurite and self.base_azurite_cmd:
                azurite_start_cmd = self.base_azurite_cmd + common_opts
                logging.info(
                    "Trying to create Azurite instance by command %s",
                    " ".join(map(str, azurite_start_cmd)),
                )

                def logging_azurite_initialization(exception, retry_number, sleep_time):
                    logging.info(
                        f"Azurite initialization failed with error: {exception}"
                    )

                retry(
                    log_function=logging_azurite_initialization,
                )(run_and_check, azurite_start_cmd)
                self.up_called = True
                logging.info("Trying to connect to Azurite")
                self.wait_azurite_to_start()

            if self.with_cassandra and self.base_cassandra_cmd:
                subprocess_check_call(self.base_cassandra_cmd + ["up", "-d"])
                self.up_called = True
                self.wait_cassandra_to_start()

            if self.with_ldap and self.base_ldap_cmd:
                ldap_start_cmd = self.base_ldap_cmd + common_opts
                subprocess_check_call(ldap_start_cmd)
                self.up_called = True
                self.wait_ldap_to_start()

            if self.with_jdbc_bridge and self.base_jdbc_bridge_cmd:
                os.makedirs(self.jdbc_driver_logs_dir)
                os.chmod(self.jdbc_driver_logs_dir, stat.S_IRWXU | stat.S_IRWXO)

                subprocess_check_call(self.base_jdbc_bridge_cmd + ["up", "-d"])
                self.up_called = True
                self.jdbc_bridge_ip = self.get_instance_ip(self.jdbc_bridge_host)
                self.wait_for_url(
                    f"http://{self.jdbc_bridge_ip}:{self.jdbc_bridge_port}/ping"
                )

            if self.with_prometheus and self.base_prometheus_cmd:
                os.makedirs(self.prometheus_writer_logs_dir)
                os.chmod(self.prometheus_writer_logs_dir, stat.S_IRWXU | stat.S_IRWXO)
                os.makedirs(self.prometheus_reader_logs_dir)
                os.chmod(self.prometheus_reader_logs_dir, stat.S_IRWXU | stat.S_IRWXO)

                prometheus_start_cmd = self.base_prometheus_cmd + common_opts

                logging.info(
                    "Trying to create Prometheus instances by command %s",
                    " ".join(map(str, prometheus_start_cmd)),
                )
                run_and_check(prometheus_start_cmd)
                self.up_called = True
                logging.info("Trying to connect to Prometheus...")
                self.wait_prometheus_to_start()

            clickhouse_start_cmd = self.base_cmd + ["up", "-d", "--no-recreate"]
            logging.debug(
                (
                    "Trying to create ClickHouse instance by command %s",
                    " ".join(map(str, clickhouse_start_cmd)),
                )
            )
            self.up_called = True
            run_and_check(clickhouse_start_cmd)
            logging.debug("ClickHouse instance created")

            start_timeout = 300.0  # seconds
            for instance in self.instances.values():
                instance.docker_client = self.docker_client
                instance.ip_address = self.get_instance_ip(instance.name)
                instance.ipv6_address = self.get_instance_global_ipv6(instance.name)

                logging.debug(
                    f"Waiting for ClickHouse start in {instance.name}, ip: {instance.ip_address}..."
                )
                instance.wait_for_start(start_timeout)
                logging.debug(f"ClickHouse {instance.name} started")

                instance.client = Client(
                    instance.ip_address, command=self.client_bin_path
                )

            self.is_up = True
            self.save_logs()

        except BaseException as e:
            logging.debug("Failed to start cluster: ")
            logging.debug(str(e))
            logging.debug(traceback.format_exc())
            self.save_logs()
            self.shutdown()
            raise

    def save_logs(self) -> None:
        # Launch the `docker-compose logs` in background to collect all the logs
        # into the docker_logs_path file during the run
        # Create directory log
        os.makedirs(p.dirname(self.docker_logs_path), exist_ok=True)
        # Here errors='replace' because docker can sometimes write non-unicode characters to its output.
        docker_logs_path = open(self.docker_logs_path, "w+", errors="replace")
        self.docker_logs_proc = subprocess.Popen(
            self.base_cmd + ["logs", "--follow"],
            stdout=docker_logs_path,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )

    def shutdown(self, kill=True, ignore_fatal=True):
        sanitizer_assert_instance = None
        fatal_log = None

        if self.up_called:
            if kill:
                try:
                    run_and_check(self.base_cmd + ["stop", "--timeout", "20"])
                except Exception as e:
                    logging.debug(
                        "Kill command failed during shutdown. {}".format(repr(e))
                    )
                    logging.debug("Trying to kill forcefully")
                    run_and_check(self.base_cmd + ["kill"])

            # Check server logs for Fatal messages and sanitizer failures.
            # NOTE: we cannot do this via docker since in case of Fatal message container may already die.
            for name, instance in self.instances.items():
                if instance.contains_in_log(
                    SANITIZER_SIGN, from_host=True, filename="stderr.log"
                ):
                    sanitizer_assert_instance = instance.grep_in_log(
                        SANITIZER_SIGN,
                        from_host=True,
                        filename="stderr.log",
                        after=1000,
                    )
                    logging.error(
                        "Sanitizer in instance %s log %s",
                        name,
                        sanitizer_assert_instance,
                    )

                if not ignore_fatal and instance.contains_in_log(
                    "Fatal", from_host=True
                ):
                    fatal_log = instance.grep_in_log("Fatal", from_host=True)
                    if "Child process was terminated by signal 9 (KILL)" in fatal_log:
                        fatal_log = None
                        continue
                    logging.error("Crash in instance %s fatal log %s", name, fatal_log)

            try:
                subprocess_check_call(self.base_cmd + ["down", "--volumes"])
            except Exception as e:
                logging.debug(
                    "Down + remove orphans failed during shutdown. {}".format(repr(e))
                )

            # Finish `docker compose logs --follow` process, just in case
            # It should be already finished because of the `docker compose down`
            if self.docker_logs_proc is not None:
                self.docker_logs_proc.kill()

            if not sanitizer_assert_instance:
                # Search for sinitizer signs in docker.log if it's still empty
                with open(self.docker_logs_path, "r") as f:
                    for line in f:
                        if SANITIZER_SIGN in line:
                            sanitizer_assert_instance = line.split("|")[0].strip()
                            break
        else:
            logging.warning(
                "docker compose up was not called. Trying to export docker.log for running containers"
            )

        self.cleanup()

        self.is_up = False

        self.docker_client = None

        for instance in list(self.instances.values()):
            instance.docker_client = None
            instance.ip_address = None
            instance.client = None

        if sanitizer_assert_instance is not None:
            raise Exception(
                "Sanitizer assert found for instance {}".format(
                    sanitizer_assert_instance
                )
            )
        if fatal_log is not None:
            raise Exception("Fatal messages found: {}".format(fatal_log))

    def _pause_container(self, instance_name):
        subprocess_check_call(self.base_cmd + ["pause", instance_name])

    def _unpause_container(self, instance_name):
        subprocess_check_call(self.base_cmd + ["unpause", instance_name])

    @contextmanager
    def pause_container(self, instance_name):
        """Use it as following:
        with cluster.pause_container(name):
            useful_stuff()
        """
        self._pause_container(instance_name)
        try:
            yield
        finally:
            self._unpause_container(instance_name)

    def open_bash_shell(self, instance_name):
        os.system(" ".join(self.base_cmd + ["exec", instance_name, "/bin/bash"]))

    def get_kazoo_client(
        self, zoo_instance_name, timeout: float = 30.0, retries=10, external_port=None
    ):
        use_ssl = False
        if self.with_zookeeper_secure:
            port = self.zookeeper_secure_port
            use_ssl = True
        elif self.with_zookeeper:
            port = self.zookeeper_port
        elif external_port is not None:
            port = external_port
        else:
            raise Exception("Cluster has no ZooKeeper")

        ip = self.get_instance_ip(zoo_instance_name)
        logging.debug(
            f"get_kazoo_client: {zoo_instance_name}, ip:{ip}, port:{port}, use_ssl:{use_ssl}"
        )
        kazoo_retry = {
            "max_tries": retries,
        }
        zk = KazooClientWithImplicitRetries(
            hosts=f"{ip}:{port}",
            timeout=timeout,
            connection_retry=kazoo_retry,
            command_retry=kazoo_retry,
            use_ssl=use_ssl,
            verify_certs=False,
            certfile=self.zookeeper_certfile,
            keyfile=self.zookeeper_keyfile,
        )
        zk.start()
        return zk

    def run_kazoo_commands_with_retries(
        self,
        kazoo_callback,
        zoo_instance_name=ZOOKEEPER_CONTAINERS[0],
        repeats=1,
        sleep_for=1,
    ):
        zk = self.get_kazoo_client(zoo_instance_name)
        logging.debug(
            f"run_kazoo_commands_with_retries: {zoo_instance_name}, {kazoo_callback}"
        )
        for i in range(repeats - 1):
            try:
                kazoo_callback(zk)
                return
            except KazooException as e:
                logging.debug(repr(e))
                time.sleep(sleep_for)
        kazoo_callback(zk)
        zk.stop()

    def add_zookeeper_startup_command(self, command):
        self.pre_zookeeper_commands.append(command)

    def stop_zookeeper_nodes(self, zk_nodes):
        for n in zk_nodes:
            logging.info("Stopping zookeeper node: %s", n)
            subprocess_check_call(self.base_zookeeper_cmd + ["stop", n])

    def process_integration_nodes(self, integration: str, nodes: list, action: str):
        base_cmd = getattr(self, f"base_{integration}_cmd")

        def process_single_node(node):
            logging.info("%sing %s node: %s", action.capitalize(), integration, node)
            subprocess_check_call(base_cmd + [action, node])
            logging.info("%sed %s node: %s", action.capitalize(), integration, node)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as executor:
            futures = []
            for n in nodes:
                futures += [executor.submit(process_single_node, n)]

            for future in concurrent.futures.as_completed(futures):
                future.result()

    # Faster than waiting for clean stop
    def kill_zookeeper_nodes(self, zk_nodes):
        self.process_integration_nodes("zookeeper", zk_nodes, "kill")

    def start_zookeeper_nodes(self, zk_nodes):
        self.process_integration_nodes("zookeeper", zk_nodes, "start")

    def query_all_nodes(self, sql, *args, **kwargs):
        return {
            name: instance.query(sql, ignore_error=True, *args, **kwargs)
            for name, instance in self.instances.items()
        }


DOCKER_COMPOSE_TEMPLATE = """---
services:
    {name}:
        image: {image}:{tag}
        hostname: {hostname}
        volumes:
            - {instance_config_dir}:/etc/clickhouse-server/
            - {db_dir}:/var/lib/clickhouse/
            - {logs_dir}:/var/log/clickhouse-server/
            - /etc/passwd:/etc/passwd:ro
            {binary_volume}
            {external_dirs_volumes}
            {odbc_ini_path}
            {keytab_path}
            {krb5_conf}
        entrypoint: {entrypoint_cmd}
        tmpfs: {tmpfs}
        {mem_limit}
        cap_add:
            - SYS_PTRACE
            - NET_ADMIN
            - IPC_LOCK
            - SYS_NICE
            # for umount/mount on fly
            - SYS_ADMIN
        depends_on: {depends_on}
        user: '{user}'
        env_file:
            - {env_file}
        security_opt:
            - label:disable
            - seccomp:unconfined
        dns_opt:
            - attempts:2
            - timeout:1
            - inet6
            - rotate
        {networks}
            {app_net}
                {ipv4_address}
                {ipv6_address}
                {net_aliases}
                    {net_alias1}
        init: {init_flag}
"""


class ClickHouseInstance:
    def __init__(
        self,
        cluster,
        base_path,
        name,
        base_config_dir,
        custom_main_configs,
        custom_user_configs,
        custom_dictionaries,
        macros,
        with_zookeeper,
        zookeeper_config_path,
        with_mysql_client,
        with_mysql57,
        with_mysql8,
        with_mysql_cluster,
        with_kafka,
        with_kafka_sasl,
        with_kerberized_kafka,
        with_kerberos_kdc,
        with_rabbitmq,
        with_nats,
        with_nginx,
        with_secrets,
        with_mongo,
        with_redis,
        with_minio,
        with_remote_database_disk,
        with_azurite,
        with_jdbc_bridge,
        with_hive,
        with_coredns,
        with_cassandra,
        with_ldap,
        with_iceberg_catalog,
        with_glue_catalog,
        with_hms_catalog,
        use_old_analyzer,
        use_distributed_plan,
        server_bin_path,
        clickhouse_path_dir,
        with_odbc_drivers,
        with_postgres,
        with_postgres_cluster,
        with_postgresql_java_client,
        clickhouse_start_command=CLICKHOUSE_START_COMMAND,
        clickhouse_start_extra_args="",
        main_config_name="config.xml",
        users_config_name="users.xml",
        copy_common_configs=True,
        hostname=None,
        env_variables=None,
        instance_env_variables=False,
        image="clickhouse/integration-test",
        tag="latest",
        stay_alive=False,
        ipv4_address=None,
        ipv6_address=None,
        with_installed_binary=False,
        external_dirs=None,
        tmpfs=None,
        mem_limit=None,
        config_root_name="clickhouse",
        extra_configs=[],
        randomize_settings=True,
        use_docker_init_flag=False,
        with_dolor=False,
        extra_parameters=None,
    ):
        self.name = name
        self.base_cmd = cluster.base_cmd
        self.docker_id = cluster.get_instance_docker_id(self.name)
        self.cluster = cluster  # type: ClickHouseCluster
        self.hostname = hostname if hostname is not None else self.name

        self.external_dirs = external_dirs
        self.tmpfs = tmpfs or []
        if mem_limit is not None:
            self.mem_limit = "mem_limit : " + mem_limit
        else:
            self.mem_limit = ""
        self.base_config_dir = (
            p.abspath(p.join(base_path, base_config_dir)) if base_config_dir else None
        )
        self.custom_main_config_paths = [
            p.abspath(p.join(base_path, c)) for c in custom_main_configs
        ]
        self.custom_user_config_paths = [
            p.abspath(p.join(base_path, c)) for c in custom_user_configs
        ]
        self.custom_dictionaries_paths = [
            p.abspath(p.join(base_path, c)) for c in custom_dictionaries
        ]
        self.custom_extra_config_paths = [
            p.abspath(p.join(base_path, c)) for c in extra_configs
        ]
        self.clickhouse_path_dir = (
            p.abspath(p.join(base_path, clickhouse_path_dir))
            if clickhouse_path_dir
            else None
        )
        self.secrets_dir = p.abspath(p.join(base_path, "secrets"))
        self.macros = macros if macros is not None else {}
        if with_remote_database_disk:
            if "shard" not in self.macros:
                self.macros["shard"] = "default"
            if "replica" not in self.macros:
                self.macros["replica"] = self.name
        self.with_zookeeper = with_zookeeper
        self.zookeeper_config_path = zookeeper_config_path

        self.server_bin_path = server_bin_path

        self.with_mysql_client = with_mysql_client
        self.with_mysql57 = with_mysql57
        self.with_mysql8 = with_mysql8
        self.with_mysql_cluster = with_mysql_cluster
        self.with_postgres = with_postgres
        self.with_postgres_cluster = with_postgres_cluster
        self.with_postgresql_java_client = with_postgresql_java_client
        self.with_kafka = with_kafka
        self.with_kafka_sasl = with_kafka_sasl
        self.with_kerberized_kafka = with_kerberized_kafka
        self.with_kerberos_kdc = with_kerberos_kdc
        self.with_rabbitmq = with_rabbitmq
        self.with_nats = with_nats
        self.with_nginx = with_nginx
        self.with_secrets = with_secrets
        self.with_mongo = with_mongo
        self.mongo_secure_config_dir = p.abspath(
            p.join(base_path, "mongo_secure_config")
        )
        self.with_redis = with_redis
        self.with_minio = with_minio
        self.with_remote_database_disk = with_remote_database_disk
        self.with_azurite = with_azurite
        self.with_cassandra = with_cassandra
        self.with_ldap = with_ldap
        self.with_jdbc_bridge = with_jdbc_bridge
        self.with_hive = with_hive
        self.with_coredns = with_coredns
        self.coredns_config_dir = p.abspath(p.join(base_path, "coredns_config"))
        self.use_old_analyzer = use_old_analyzer
        self.use_distributed_plan = use_distributed_plan
        self.randomize_settings = randomize_settings

        self.main_config_name = main_config_name
        self.users_config_name = users_config_name
        self.copy_common_configs = copy_common_configs

        clickhouse_start_command_with_conf = clickhouse_start_command.replace(
            "{main_config_file}", self.main_config_name
        )

        self.clickhouse_start_command = "{} -- {}".format(
            clickhouse_start_command_with_conf, clickhouse_start_extra_args
        )
        self.clickhouse_start_command_in_daemon = "{} --daemon -- {}".format(
            clickhouse_start_command_with_conf, clickhouse_start_extra_args
        )
        self.clickhouse_stay_alive_command = "bash -c \"trap 'pkill tail' INT TERM; {}; coproc tail -f /dev/null; wait $$!\"".format(
            self.clickhouse_start_command_in_daemon
        )

        self.path = p.join(self.cluster.instances_dir, name)
        self.docker_compose_path = p.join(self.path, "docker-compose.yml")
        self.env_variables = env_variables or {}
        self.instance_env_variables = instance_env_variables
        self.env_file = self.cluster.env_file
        if with_odbc_drivers:
            self.odbc_ini_path = self.path + "/odbc.ini:/etc/odbc.ini"
            self.with_mysql8 = True
        else:
            self.odbc_ini_path = ""

        if with_kerberized_kafka or with_kerberos_kdc:
            if with_kerberos_kdc:
                base_secrets_dir = self.cluster.instances_dir
            else:
                base_secrets_dir = os.path.dirname(self.docker_compose_path)
            self.keytab_path = "- " + base_secrets_dir + "/secrets:/tmp/keytab"
            self.krb5_conf = (
                "- " + base_secrets_dir + "/secrets/krb.conf:/etc/krb5.conf:ro"
            )
        else:
            self.keytab_path = ""
            self.krb5_conf = ""

        self.docker_client = None
        self.ip_address = None
        self.client = None
        self.image = image
        self.tag = tag
        self.stay_alive = stay_alive
        self.ipv4_address = ipv4_address
        self.ipv6_address = ipv6_address
        self.with_installed_binary = with_installed_binary
        self.is_up = False
        self.config_root_name = config_root_name
        self.docker_init_flag = use_docker_init_flag
        self.with_dolor = with_dolor

    def is_built_with_sanitizer(self, sanitizer_name=""):
        build_opts = self.query(
            "SELECT value FROM system.build_options WHERE name = 'CXX_FLAGS'"
        )
        return "-fsanitize={}".format(sanitizer_name) in build_opts

    def is_debug_build(self):
        build_opts = self.query(
            "SELECT value FROM system.build_options WHERE name = 'CXX_FLAGS'"
        )
        return "NDEBUG" not in build_opts

    def is_built_with_thread_sanitizer(self):
        return self.is_built_with_sanitizer("thread")

    def is_built_with_address_sanitizer(self):
        return self.is_built_with_sanitizer("address")

    def is_built_with_memory_sanitizer(self):
        return self.is_built_with_sanitizer("memory")

    # Connects to the instance via clickhouse-client, sends a query (1st argument) and returns the answer
    def query(
        self,
        sql,
        stdin=None,
        timeout=None,
        settings=None,
        user=None,
        password=None,
        database=None,
        host=None,
        ignore_error=False,
        query_id=None,
        parse=False,
    ):
        sql_for_log = ""
        if len(sql) > 1000:
            sql_for_log = sql[:1000]
        else:
            sql_for_log = sql
        logging.debug("Executing query %s on %s", sql_for_log, self.name)
        return self.client.query(
            sql,
            stdin=stdin,
            timeout=timeout,
            settings=settings,
            user=user,
            password=password,
            database=database,
            ignore_error=ignore_error,
            query_id=query_id,
            host=host,
            parse=parse,
        )

    def query_with_retry(
        self,
        sql,
        stdin=None,
        timeout=None,
        settings=None,
        user=None,
        password=None,
        database=None,
        host=None,
        ignore_error=False,
        retry_count=20,
        sleep_time=0.5,
        check_callback=lambda x: True,
        parse=False,
    ):
        # logging.debug(f"Executing query {sql} on {self.name}")
        result = None
        exception_msg = ""
        for i in range(retry_count):
            try:
                result = self.query(
                    sql,
                    stdin=stdin,
                    timeout=timeout,
                    settings=settings,
                    user=user,
                    password=password,
                    database=database,
                    host=host,
                    ignore_error=ignore_error,
                    parse=parse,
                )
                if check_callback(result):
                    return result
                time.sleep(sleep_time)
            except QueryRuntimeException as ex:
                exception_msg = f"{type(ex).__name__}: {str(ex)}"
                # Container is down, this is likely due to server crash.
                if "No route to host" in str(ex):
                    raise
                time.sleep(sleep_time)
            except Exception as ex:
                # logging.debug("Retry {} got exception {}".format(i + 1, ex))
                exception_msg = f"{type(ex).__name__}: {str(ex)}"
                time.sleep(sleep_time)

        if result is not None:
            return result
        raise Exception(f"Can't execute query {sql}\n{exception_msg}")

    # As query() but doesn't wait response and returns response handler
    def get_query_request(self, sql, *args, **kwargs):
        logging.debug(f"Executing query {sql} on {self.name}")
        return self.client.get_query_request(sql, *args, **kwargs)

    # Connects to the instance via clickhouse-client, sends a query (1st argument), expects an error and return its code
    def query_and_get_error(
        self,
        sql,
        stdin=None,
        timeout=None,
        settings=None,
        user=None,
        password=None,
        database=None,
        query_id=None,
    ):
        logging.debug(f"Executing query {sql} on {self.name}")
        return self.client.query_and_get_error(
            sql,
            stdin=stdin,
            timeout=timeout,
            settings=settings,
            user=user,
            password=password,
            database=database,
            query_id=query_id,
        )

    def query_and_get_error_with_retry(
        self,
        sql,
        stdin=None,
        timeout=None,
        settings=None,
        user=None,
        password=None,
        database=None,
        retry_count=20,
        sleep_time=0.5,
    ):
        logging.debug(f"Executing query {sql} on {self.name}")
        result = None
        for i in range(retry_count):
            try:
                result = self.client.query_and_get_error(
                    sql,
                    stdin=stdin,
                    timeout=timeout,
                    settings=settings,
                    user=user,
                    password=password,
                    database=database,
                )
                time.sleep(sleep_time)

                if result is not None:
                    return result
            except QueryRuntimeException as ex:
                logging.debug("Retry {} got exception {}".format(i + 1, ex))
                time.sleep(sleep_time)

        raise Exception("Query {} did not fail".format(sql))

    # The same as query_and_get_error but ignores successful query.
    def query_and_get_answer_with_error(
        self,
        sql,
        stdin=None,
        timeout=None,
        settings=None,
        user=None,
        password=None,
        database=None,
        query_id=None,
    ):
        logging.debug(f"Executing query {sql} on {self.name}")
        return self.client.query_and_get_answer_with_error(
            sql,
            stdin=stdin,
            timeout=timeout,
            settings=settings,
            user=user,
            password=password,
            database=database,
            query_id=query_id,
        )

    # Connects to the instance via HTTP interface, sends a query and returns the answer
    def http_query(
        self,
        sql,
        data=None,
        method=None,
        params=None,
        user=None,
        password=None,
        port=8123,
        timeout=None,
        retry_strategy=None,
        content=False,
    ):
        output, error = self.http_query_and_get_answer_with_error(
            sql,
            data=data,
            method=method,
            params=params,
            user=user,
            password=password,
            port=port,
            timeout=timeout,
            retry_strategy=retry_strategy,
            content=content,
        )

        if error:
            raise Exception("ClickHouse HTTP server returned " + error)

        return output

    # Connects to the instance via HTTP interface, sends a query, expects an error and return the error message
    def http_query_and_get_error(
        self,
        sql,
        data=None,
        method=None,
        params=None,
        user=None,
        password=None,
        port=8123,
        timeout=None,
        retry_strategy=None,
    ):
        output, error = self.http_query_and_get_answer_with_error(
            sql,
            data=data,
            method=method,
            params=params,
            user=user,
            password=password,
            port=port,
            timeout=timeout,
            retry_strategy=retry_strategy,
        )

        if not error:
            raise Exception(
                "ClickHouse HTTP server is expected to fail, but succeeded: " + output
            )

        return error

    def append_hosts(self, name, ip):
        self.exec_in_container(
            (["bash", "-c", "echo '{}' {} >> /etc/hosts".format(ip, name)]),
            privileged=True,
            user="root",
        )

    def set_hosts(self, hosts):
        entries = ["127.0.0.1 localhost", "::1 localhost"]
        for host in hosts:
            entries.append(f"{host[0]} {host[1]}")

        self.exec_in_container(
            ["bash", "-c", 'echo -e "{}" > /etc/hosts'.format("\\n".join(entries))],
            privileged=True,
            user="root",
        )

    # Connects to the instance via HTTP interface, sends a query and returns both the answer and the error message
    # as a tuple (output, error).
    def http_query_and_get_answer_with_error(
        self,
        sql,
        data=None,
        method=None,
        params=None,
        user=None,
        password=None,
        port=8123,
        timeout=None,
        retry_strategy=None,
        content=False,
    ):
        logging.debug(f"Executing query {sql} on {self.name} via HTTP interface")
        if params is None:
            params = {}
        else:
            params = params.copy()

        if sql is not None:
            params["query"] = sql

        auth = None
        if user and password:
            auth = requests.auth.HTTPBasicAuth(user, password)
        elif user:
            auth = requests.auth.HTTPBasicAuth(user, "")
        url = f"http://{self.ip_address}:{port}/?" + urllib.parse.urlencode(params)

        if retry_strategy is None:
            requester = requests
        else:
            adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy)
            requester = requests.Session()
            requester.mount("https://", adapter)
            requester.mount("http://", adapter)

        if method is None:
            method = "POST" if data else "GET"

        r = requester.request(method, url, data=data, auth=auth, timeout=timeout)
        # Force encoding to UTF-8
        r.encoding = "UTF-8"

        if r.ok:
            return (r.content if content else r.text, None)

        code = r.status_code
        return (None, str(code) + " " + http.client.responses[code] + ": " + r.text)

    # Connects to the instance via HTTP interface, sends a query and returns the answer
    def http_request(self, url, method="GET", params=None, data=None, headers=None):
        logging.debug(f"Sending HTTP request {url} to {self.name}")
        url = "http://" + self.ip_address + ":8123/" + url
        return requests.request(
            method=method, url=url, params=params, data=data, headers=headers
        )

    def stop_clickhouse(self, stop_wait_sec=30, kill=False):
        if not self.stay_alive:
            raise Exception(
                "clickhouse can be stopped only with stay_alive=True instance"
            )
        try:
            ps_clickhouse = self.exec_in_container(
                ["bash", "-c", "ps -C clickhouse"], nothrow=True, user="root"
            )
            if ps_clickhouse == "  PID TTY      STAT   TIME COMMAND":
                logging.warning("ClickHouse process already stopped")
                return

            self.exec_in_container(
                ["bash", "-c", "pkill {} clickhouse".format("-9" if kill else "")],
                user="root",
            )

            start_time = time.time()
            stopped = False
            while time.time() <= start_time + stop_wait_sec:
                pid = self.get_process_pid("clickhouse")
                if pid is None:
                    stopped = True
                    break
                else:
                    time.sleep(1)

            if not stopped:
                pid = self.get_process_pid("clickhouse")
                if pid is not None:
                    logging.warning(
                        f"Force kill clickhouse in stop_clickhouse. ps:{pid}"
                    )
                    self.exec_in_container(
                        [
                            "bash",
                            "-c",
                            f"gdb -batch -ex 'thread apply all bt full' -p {pid} > /var/log/clickhouse-server/stdout.log",
                        ],
                        user="root",
                    )
                    self.stop_clickhouse(kill=True)
                else:
                    ps_all = self.exec_in_container(
                        ["bash", "-c", "ps aux"], nothrow=True, user="root"
                    )
                    logging.warning(
                        f"We want force stop clickhouse, but no clickhouse-server is running\n{ps_all}"
                    )
                    return
        except Exception as e:
            logging.warning(f"Stop ClickHouse raised an error {e}")

    def start_clickhouse(
        self, start_wait_sec=60, retry_start=True, expected_to_fail=False
    ):
        if not self.stay_alive:
            raise Exception(
                "ClickHouse can be started again only with stay_alive=True instance"
            )
        start_time = time.time()
        time_to_sleep = 0.5
        exec_id = None

        while start_time + start_wait_sec >= time.time():
            # sometimes after SIGKILL (hard reset) server may refuse to start for some time
            # for different reasons.
            pid = self.get_process_pid("clickhouse")
            if pid is None:
                logging.debug("No clickhouse process running. Start new one.")
                exec_id = self.exec_in_container(
                    ["bash", "-c", self.clickhouse_start_command],
                    user=str(os.getuid()),
                    detach=True,
                    use_cli=False,
                    get_exec_id=True,
                )
                if expected_to_fail:
                    self.wait_start_failed(start_wait_sec + start_time - time.time())
                    return
                time.sleep(1)
                continue
            else:
                logging.debug("Clickhouse process running.")
                if expected_to_fail:
                    raise Exception("ClickHouse was expected not to be running.")
                try:
                    self.wait_start(start_wait_sec + start_time - time.time())
                    return exec_id
                except Exception as e:
                    logging.warning(
                        f"Current start attempt failed. Will kill {pid} just in case."
                    )
                    self.exec_in_container(
                        ["bash", "-c", f"kill -9 {pid}"], user="root", nothrow=True
                    )
                    if not retry_start:
                        raise
                    time.sleep(time_to_sleep)

        raise Exception("Cannot start ClickHouse, see additional info in logs")

    def wait_start(self, start_wait_sec):
        start_time = time.time()
        last_err = None
        while True:
            try:
                pid = self.get_process_pid("clickhouse")
                if pid is None:
                    raise Exception("ClickHouse server is not running. Check logs.")
                exec_query_with_retry(self, "select 20", retry_count=10, silent=True)
                return
            except QueryRuntimeException as err:
                last_err = err
                pid = self.get_process_pid("clickhouse")
                if pid is not None:
                    logging.warning(f"ERROR {err}")
                else:
                    raise Exception("ClickHouse server is not running. Check logs.")
            if time.time() > start_time + start_wait_sec:
                break
        logging.error(
            f"No time left to start. But process is still running. Will dump threads."
        )
        ps_clickhouse = self.exec_in_container(
            ["bash", "-c", "ps -C clickhouse"], nothrow=True, user="root"
        )
        logging.info(f"PS RESULT:\n{ps_clickhouse}")
        pid = self.get_process_pid("clickhouse")
        if pid is not None:
            self.exec_in_container(
                ["bash", "-c", f"gdb -batch -ex 'thread apply all bt full' -p {pid}"],
                user="root",
            )
        if last_err is not None:
            raise last_err

    def wait_start_failed(self, start_wait_sec):
        start_time = time.time()
        while time.time() <= start_time + start_wait_sec:
            pid = self.get_process_pid("clickhouse")
            if pid is None:
                return
            time.sleep(1)
        logging.error(
            f"No time left to shutdown. Process is still running. Will dump threads."
        )
        ps_clickhouse = self.exec_in_container(
            ["bash", "-c", "ps -C clickhouse"], nothrow=True, user="root"
        )
        logging.info(f"PS RESULT:\n{ps_clickhouse}")
        pid = self.get_process_pid("clickhouse")
        if pid is not None:
            self.exec_in_container(
                ["bash", "-c", f"gdb -batch -ex 'thread apply all bt full' -p {pid}"],
                user="root",
            )
        raise Exception(
            "ClickHouse server is still running, but was expected to shutdown. Check logs."
        )

    def restart_clickhouse(self, stop_start_wait_sec=60, kill=False):
        self.stop_clickhouse(stop_start_wait_sec, kill)
        self.start_clickhouse(stop_start_wait_sec)

    def exec_in_container(
        self,
        cmd: Sequence[str],
        detach: bool = False,
        nothrow: bool = False,
        **kwargs: Any,
    ) -> str:
        return self.cluster.exec_in_container(
            self.docker_id, cmd, detach, nothrow, **kwargs
        )

    def rotate_logs(self):
        self.exec_in_container(
            ["bash", "-c", f"kill -HUP {self.get_process_pid('clickhouse server')}"],
            user="root",
        )

    def contains_in_log(
        self,
        substring,
        from_host=False,
        filename="clickhouse-server.log",
        exclusion_substring="",
    ):
        if from_host:
            # We check first file exists but want to look for all rotated logs as well
            result = subprocess_check_call(
                [
                    "bash",
                    "-c",
                    f'[ -f {self.logs_dir}/{filename} ] && zgrep -aH "{substring}" {self.logs_dir}/{filename}* | ( [ -z "{exclusion_substring}" ] && cat || grep -v "${exclusion_substring}" ) || true',
                ]
            )
        else:
            result = self.exec_in_container(
                [
                    "bash",
                    "-c",
                    f'[ -f /var/log/clickhouse-server/{filename} ] && zgrep -aH "{substring}" /var/log/clickhouse-server/{filename} | ( [ -z "{exclusion_substring}" ] && cat || grep -v "${exclusion_substring}" ) || true',
                ]
            )
        return len(result) > 0

    def grep_in_log(
        self, substring, from_host=False, filename="clickhouse-server.log", after=None
    ):
        logging.debug(f"grep in log called %s", substring)
        if after is not None:
            after_opt = "-A{}".format(after)
        else:
            after_opt = ""
        if from_host:
            # We check fist file exists but want to look for all rotated logs as well
            result = subprocess_check_call(
                [
                    "bash",
                    "-c",
                    f'[ -f {self.logs_dir}/{filename} ] && zgrep {after_opt} -a "{substring}" {self.logs_dir}/{filename}* || true',
                ]
            )
        else:
            result = self.exec_in_container(
                [
                    "bash",
                    "-c",
                    f'[ -f /var/log/clickhouse-server/{filename} ] && zgrep {after_opt} -a "{substring}" /var/log/clickhouse-server/{filename}* || true',
                ]
            )
        logging.debug("grep result %s", result)
        return result

    def count_in_log(self, substring):
        result = self.exec_in_container(
            [
                "bash",
                "-c",
                'grep -a "{}" /var/log/clickhouse-server/clickhouse-server.log | wc -l'.format(
                    substring
                ),
            ]
        )
        return result

    def wait_for_log_line(
        self,
        regexp,
        filename="/var/log/clickhouse-server/clickhouse-server.log",
        timeout=30,
        repetitions=1,
        look_behind_lines=100,
    ):
        start_time = time.time()
        result = self.exec_in_container(
            [
                "bash",
                "-c",
                'timeout {} tail -Fn{} "{}" | grep -Em {} {}'.format(
                    timeout,
                    look_behind_lines,
                    filename,
                    repetitions,
                    shlex.quote(regexp),
                ),
            ]
        )

        # if repetitions>1 grep will return success even if not enough lines were collected,
        if repetitions > 1 and len(result.splitlines()) < repetitions:
            logging.debug(
                "wait_for_log_line: those lines were found during {} seconds:".format(
                    timeout
                )
            )
            logging.debug(result)
            raise Exception(
                "wait_for_log_line: Not enough repetitions: {} found, while {} expected".format(
                    len(result.splitlines()), repetitions
                )
            )

        wait_duration = time.time() - start_time

        logging.debug(
            '{} log line(s) matching "{}" appeared in a {:.3f} seconds'.format(
                repetitions, regexp, wait_duration
            )
        )
        return wait_duration

    def path_exists(self, path):
        return (
            self.exec_in_container(
                [
                    "bash",
                    "-c",
                    "echo $(if [ -e '{}' ]; then echo 'yes'; else echo 'no'; fi)".format(
                        path
                    ),
                ]
            )
            == "yes\n"
        )

    def copy_file_to_container(self, local_path, dest_path):
        return self.cluster.copy_file_to_container(
            self.docker_id, local_path, dest_path
        )

    def copy_file_from_container(self, dest_path, local_path):
        return self.cluster.copy_file_from_container(
            self.docker_id, dest_path, local_path
        )

    def file_exists_in_container(self, path):
        return self.cluster.file_exists_in_container(
            self.docker_id, path
        )
    
    def get_files_list_in_container(self, path):
        return self.cluster.get_files_list_in_container(
            self.docker_id, path
        )

    def move_file_in_container(self, old_path, new_path):
        return self.cluster.move_file_in_container(self.docker_id, old_path, new_path)

    def remove_file_from_container(self, path):
        return self.cluster.remove_file_from_container(self.docker_id, path)

    def get_process_pid(self, process_name):
        output = self.exec_in_container(
            [
                "bash",
                "-c",
                "ps ax | grep '{}' | grep -v 'grep' | grep -v 'coproc' | grep -v 'bash -c' | awk '{{print $1}}'".format(
                    process_name
                ),
            ]
        )
        if output:
            try:
                pid = int(output.split("\n")[0].strip())
                return pid
            except:
                return None
        return None

    def restart_with_original_version(
        self,
        stop_start_wait_sec=300,
        callback_onstop=None,
        signal=15,
        clear_data_dir=False,
    ):
        begin_time = time.time()
        if not self.stay_alive:
            raise Exception("Cannot restart not stay alive container")
        self.exec_in_container(
            ["bash", "-c", "pkill -{} clickhouse".format(signal)], user="root"
        )
        retries = int(stop_start_wait_sec / 0.5)
        local_counter = 0
        # wait stop
        while local_counter < retries:
            if not self.get_process_pid("clickhouse server"):
                break
            time.sleep(0.5)
            local_counter += 1

        # force kill if server hangs
        if self.get_process_pid("clickhouse server"):
            # server can die before kill, so don't throw exception, it's expected
            self.exec_in_container(
                ["bash", "-c", "pkill -{} clickhouse".format(9)],
                nothrow=True,
                user="root",
            )

        if callback_onstop:
            callback_onstop(self)

        if clear_data_dir:
            self.exec_in_container(
                [
                    "bash",
                    "-c",
                    "rm -rf /var/lib/clickhouse/metadata && rm -rf /var/lib/clickhouse/data",
                ],
                user="root",
            )

        self.exec_in_container(
            [
                "bash",
                "-c",
                "echo 'restart_with_original_version: From version' && /usr/bin/clickhouse server --version && echo 'To version' && /usr/share/clickhouse_original server --version",
            ]
        )
        self.exec_in_container(
            [
                "bash",
                "-c",
                "cp /usr/share/clickhouse_original /usr/bin/clickhouse && chmod 777 /usr/bin/clickhouse",
            ],
            user="root",
        )
        self.exec_in_container(
            ["bash", "-c", self.clickhouse_start_command_in_daemon],
            user=str(os.getuid()),
        )

        # wait start
        time_left = begin_time + stop_start_wait_sec - time.time()
        if time_left <= 0:
            raise Exception(f"No time left during restart")
        else:
            self.wait_start(time_left)

    def restart_with_latest_version(
        self,
        stop_start_wait_sec=300,
        callback_onstop=None,
        signal=15,
        fix_metadata=False,
    ):
        begin_time = time.time()
        if not self.stay_alive:
            raise Exception("Cannot restart not stay alive container")
        self.exec_in_container(
            ["bash", "-c", "pkill -{} clickhouse".format(signal)], user="root"
        )
        retries = int(stop_start_wait_sec / 0.5)
        local_counter = 0
        # wait stop
        while local_counter < retries:
            if not self.get_process_pid("clickhouse server"):
                break
            time.sleep(0.5)
            local_counter += 1

        # force kill if server hangs
        if self.get_process_pid("clickhouse server"):
            # server can die before kill, so don't throw exception, it's expected
            self.exec_in_container(
                ["bash", "-c", "pkill -{} clickhouse".format(9)],
                nothrow=True,
                user="root",
            )

        if callback_onstop:
            callback_onstop(self)
        self.exec_in_container(
            ["bash", "-c", "cp /usr/bin/clickhouse /usr/share/clickhouse_original"],
            user="root",
        )
        self.exec_in_container(
            [
                "bash",
                "-c",
                "cp /usr/share/clickhouse_fresh /usr/bin/clickhouse && chmod 777 /usr/bin/clickhouse",
            ],
            user="root",
        )
        self.exec_in_container(
            [
                "bash",
                "-c",
                "echo 'restart_with_latest_version: From version' && /usr/share/clickhouse_original server --version && echo 'To version' /usr/share/clickhouse_fresh server --version",
            ]
        )
        if fix_metadata:
            # Versions older than 20.7 might not create .sql file for system and default database
            # Create it manually if upgrading from older version
            self.exec_in_container(
                [
                    "bash",
                    "-c",
                    "if [ ! -f /var/lib/clickhouse/metadata/system.sql ]; then echo 'ATTACH DATABASE system ENGINE=Ordinary' > /var/lib/clickhouse/metadata/system.sql; fi",
                ]
            )
            self.exec_in_container(
                [
                    "bash",
                    "-c",
                    "if [ ! -f /var/lib/clickhouse/metadata/default.sql ]; then echo 'ATTACH DATABASE system ENGINE=Ordinary' > /var/lib/clickhouse/metadata/default.sql; fi",
                ]
            )
        self.exec_in_container(
            ["bash", "-c", self.clickhouse_start_command_in_daemon],
            user=str(os.getuid()),
        )

        # wait start
        time_left = begin_time + stop_start_wait_sec - time.time()
        if time_left <= 0:
            raise Exception(f"No time left during restart")
        else:
            self.wait_start(time_left)

    def get_docker_handle(self) -> Container:
        return self.cluster.get_docker_handle(self.docker_id)

    def stop(self):
        self.get_docker_handle().stop()

    def start(self):
        self.get_docker_handle().start()

    def wait_for_start(self, start_timeout=None, connection_timeout=None):
        handle = self.get_docker_handle()

        if start_timeout is None or start_timeout <= 0:
            raise Exception("Invalid timeout: {}".format(start_timeout))

        if connection_timeout is not None and connection_timeout < start_timeout:
            raise Exception(
                "Connection timeout {} should be grater then start timeout {}".format(
                    connection_timeout, start_timeout
                )
            )

        start_time = time.time()
        prev_rows_in_log = 0

        def has_new_rows_in_log():
            nonlocal prev_rows_in_log
            try:
                rows_in_log = int(self.count_in_log(".*").strip())
                res = rows_in_log > prev_rows_in_log
                prev_rows_in_log = rows_in_log
                return res
            except ValueError:
                return False

        while True:
            handle.reload()
            status = handle.status
            if status == "exited":
                raise Exception(
                    f"Instance `{self.name}' failed to start. Container status: {status}, logs: {handle.logs().decode('utf-8')}"
                )

            deadline = start_time + start_timeout
            # It is possible that server starts slowly.
            # If container is running, and there is some progress in log, check connection_timeout.
            if connection_timeout and status == "running" and has_new_rows_in_log():
                deadline = start_time + connection_timeout

            current_time = time.time()
            if current_time >= deadline:
                raise Exception(
                    f"Timed out while waiting for instance `{self.name}' with ip address {self.ip_address} to start. "
                    f"Container status: {status}, logs: {handle.logs().decode('utf-8')}"
                )

            socket_timeout = min(start_timeout, deadline - current_time)

            # Repeatedly poll the instance address until there is something that listens there.
            # Usually it means that ClickHouse is ready to accept queries.
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(socket_timeout)
                sock.connect((self.ip_address, 9000))
                self.is_up = True
                return
            except socket.timeout:
                continue
            except socket.error as e:
                if (
                    e.errno == errno.ECONNREFUSED
                    or e.errno == errno.EHOSTUNREACH
                    or e.errno == errno.ENETUNREACH
                ):
                    time.sleep(0.1)
                else:
                    raise
            finally:
                sock.close()

    def dict_to_xml(self, dictionary):
        xml_str = dict2xml(
            dictionary, wrap=self.config_root_name, indent="  ", newlines=True
        )
        return xml_str

    def get_machine_name(self):
        return platform.machine()

    @property
    def odbc_drivers(self):
        if self.odbc_ini_path:
            return {
                "SQLite3": {
                    "DSN": "sqlite3_odbc",
                    "Database": "/tmp/sqliteodbc",
                    "Driver": f"/usr/lib/{self.get_machine_name()}-linux-gnu/odbc/libsqlite3odbc.so",
                    "Setup": f"/usr/lib/{self.get_machine_name()}-linux-gnu/odbc/libsqlite3odbc.so",
                },
                "MySQL": {
                    "DSN": "mysql_odbc",
                    "Driver": f"/usr/lib/{self.get_machine_name()}-linux-gnu/odbc/libmyodbc.so",
                    "Database": odbc_mysql_db,
                    "Uid": odbc_mysql_uid,
                    "Pwd": mysql_pass,
                    "Server": self.cluster.mysql8_host,
                },
                "PostgreSQL": {
                    "DSN": "postgresql_odbc",
                    "Database": odbc_psql_db,
                    "UserName": odbc_psql_user,
                    "Password": pg_pass,
                    "Port": str(self.cluster.postgres_port),
                    "Servername": self.cluster.postgres_host,
                    "Protocol": "9.3",
                    "ReadOnly": "No",
                    "RowVersioning": "No",
                    "ShowSystemTables": "No",
                    "Driver": f"/usr/lib/{self.get_machine_name()}-linux-gnu/odbc/psqlodbca.so",
                    "Setup": f"/usr/lib/{self.get_machine_name()}-linux-gnu/odbc/libodbcpsqlS.so",
                    "ConnSettings": "",
                },
            }
        else:
            return {}

    def _create_odbc_config_file(self):
        with open(self.odbc_ini_path.split(":")[0], "w") as f:
            for driver_setup in list(self.odbc_drivers.values()):
                f.write("[{}]\n".format(driver_setup["DSN"]))
                for key, value in list(driver_setup.items()):
                    if key != "DSN":
                        f.write(key + "=" + value + "\n")

    @contextmanager
    def with_replace_config(self, path, replacement):
        """Create a copy of existing config (if exists) and revert on leaving the context"""
        _directory, filename = os.path.split(path)
        basename, extension = os.path.splitext(filename)
        id = uuid.uuid4()
        backup_path = f"/tmp/{basename}_{id}{extension}"
        self.exec_in_container(
            ["bash", "-c", f"test ! -f {path} || mv --no-clobber {path} {backup_path}"]
        )
        self.exec_in_container(
            ["bash", "-c", "echo '{}' > {}".format(replacement, path)]
        )
        yield
        self.exec_in_container(
            ["bash", "-c", f"test ! -f {backup_path} || mv {backup_path} {path}"]
        )

    def replace_config(self, path_to_config, replacement):
        self.exec_in_container(
            ["bash", "-c", "echo '{}' > {}".format(replacement, path_to_config)]
        )

    def replace_in_config(self, path_to_config, replace, replacement):
        # Do `sed 's/{replace}/{replacement}/g'`, but with some hacks to make it work when {replace}
        # and {replacement} have quotes or slashes.
        for d in "/|#-=+@^*~":
            if d not in replace and d not in replacement:
                delimiter = d
                break
        else:
            raise Exception(f"Couldn't find a suitable delimiter")
        replace = shlex.quote(replace)
        replacement = shlex.quote(replacement)
        self.exec_in_container(
            [
                "bash",
                "-c",
                f"sed -i 's{delimiter}'{replace}'{delimiter}'{replacement}'{delimiter}g' {path_to_config}",
            ]
        )

    def create_dir(self):
        """Create the instance directory and all the needed files there."""

        os.makedirs(self.path)

        instance_config_dir = p.abspath(p.join(self.path, "configs"))
        os.makedirs(instance_config_dir)

        print(
            f"Copy common default production configuration from {self.base_config_dir}. Files: {self.main_config_name}, {self.users_config_name}"
        )

        shutil.copyfile(
            p.join(self.base_config_dir, self.main_config_name),
            p.join(instance_config_dir, self.main_config_name),
        )
        shutil.copyfile(
            p.join(self.base_config_dir, self.users_config_name),
            p.join(instance_config_dir, self.users_config_name),
        )

        logging.debug("Create directory for configuration generated in this helper")
        # used by all utils with any config
        conf_d_dir = p.abspath(p.join(instance_config_dir, "conf.d"))
        os.mkdir(conf_d_dir)

        logging.debug("Create directory for common tests configuration")
        # used by server with main config.xml
        self.config_d_dir = p.abspath(p.join(instance_config_dir, "config.d"))
        os.mkdir(self.config_d_dir)
        users_d_dir = p.abspath(p.join(instance_config_dir, "users.d"))
        os.mkdir(users_d_dir)
        dictionaries_dir = p.abspath(p.join(instance_config_dir, "dictionaries"))
        os.mkdir(dictionaries_dir)
        extra_conf_dir = p.abspath(p.join(instance_config_dir, "extra_conf.d"))
        os.mkdir(extra_conf_dir)

        def write_embedded_config(name, dest_dir, fix_log_level=False):
            with open(p.join(HELPERS_DIR, name), "r") as f:
                data = f.read()
                data = data.replace("clickhouse", self.config_root_name)
                if fix_log_level:
                    data = data.replace("<level>test</level>", "<level>trace</level>")
                with open(p.join(dest_dir, name), "w") as r:
                    r.write(data)

        logging.debug("Copy common configuration from helpers")
        # The file is named with 0_ prefix to be processed before other configuration overloads.
        if self.copy_common_configs:
            write_embedded_config(
                "0_common_instance_config.xml",
                self.config_d_dir,
                self.with_installed_binary,
            )

        if not self.with_dolor:
            write_embedded_config("0_common_instance_users.xml", users_d_dir)
            if self.with_installed_binary:
                # Ignore CPU overload in this case
                write_embedded_config(
                    "0_common_min_cpu_busy_time.xml", self.config_d_dir
                )

        use_old_analyzer = os.environ.get("CLICKHOUSE_USE_OLD_ANALYZER") is not None
        use_distributed_plan = (
            os.environ.get("CLICKHOUSE_USE_DISTRIBUTED_PLAN") is not None
        )

        # If specific version was used there can be no
        # enable_analyzer setting, so do this only if it was
        # explicitly requested.
        if self.tag:
            use_old_analyzer = False
        if self.tag != "latest":
            use_distributed_plan = False
        # Prefer specified in the test option:
        if self.use_old_analyzer is not None:
            use_old_analyzer = self.use_old_analyzer
        if self.use_distributed_plan is not None:
            use_distributed_plan = self.use_distributed_plan

        if use_old_analyzer:
            write_embedded_config("0_common_enable_old_analyzer.xml", users_d_dir)

        if use_distributed_plan:
            write_embedded_config("0_common_enable_distributed_plan.xml", users_d_dir)

        if len(self.custom_dictionaries_paths):
            write_embedded_config("0_common_enable_dictionaries.xml", self.config_d_dir)

        if (
            self.randomize_settings
            and self.image == "clickhouse/integration-test"
            and self.tag == DOCKER_BASE_TAG
            and self.base_config_dir == DEFAULT_BASE_CONFIG_DIR
        ):
            # If custom main config is used, do not apply random settings to it
            write_random_settings_config(Path(users_d_dir) / "0_random_settings.xml")

        version = None
        version_parts = self.tag.split(".")
        if version_parts[0].isdigit() and version_parts[1].isdigit():
            version = {"major": int(version_parts[0]), "minor": int(version_parts[1])}

        # async replication is only supported in version 23.9+
        # for tags that don't specify a version we assume it has a version of ClickHouse
        # that supports async replication if a test for it is present
        if (
            version == None
            or version["major"] > 23
            or (version["major"] == 23 and version["minor"] >= 9)
        ):
            write_embedded_config(
                "0_common_enable_keeper_async_replication.xml", self.config_d_dir
            )

        logging.debug("Generate and write macros file")
        macros = self.macros.copy()
        macros["instance"] = self.name
        with open(p.join(conf_d_dir, "macros.xml"), "w") as macros_config:
            macros_config.write(self.dict_to_xml({"macros": macros}))

        # Put ZooKeeper config
        if self.with_zookeeper:
            shutil.copy(self.zookeeper_config_path, conf_d_dir)

        if self.with_secrets:
            if self.with_kerberos_kdc:
                base_secrets_dir = self.cluster.instances_dir
            else:
                base_secrets_dir = self.path
            from_dir = self.secrets_dir
            to_dir = p.abspath(p.join(base_secrets_dir, "secrets"))
            logging.debug(f"Copy secret from {from_dir} to {to_dir}")
            shutil.copytree(
                self.secrets_dir,
                p.abspath(p.join(base_secrets_dir, "secrets")),
                dirs_exist_ok=True,
            )

        if self.with_mongo and os.path.exists(self.mongo_secure_config_dir):
            shutil.copytree(
                self.mongo_secure_config_dir,
                p.abspath(p.join(self.path, "mongo_secure_config")),
            )

        if self.with_coredns:
            shutil.copytree(
                self.coredns_config_dir, p.abspath(p.join(self.path, "coredns_config"))
            )

        # Copy config.d configs
        logging.debug(
            f"Copy custom test config files {self.custom_main_config_paths} to {self.config_d_dir}"
        )
        for path in self.custom_main_config_paths:
            shutil.copy(path, self.config_d_dir)

        # Copy users.d configs
        for path in self.custom_user_config_paths:
            shutil.copy(path, users_d_dir)

        # Copy dictionaries configs to configs/dictionaries
        for path in self.custom_dictionaries_paths:
            shutil.copy(path, dictionaries_dir)
        for path in self.custom_extra_config_paths:
            shutil.copy(path, extra_conf_dir)

        db_dir = p.abspath(p.join(self.path, "database"))
        logging.debug(f"Setup database dir {db_dir}")
        if self.clickhouse_path_dir is not None:
            logging.debug(f"Database files taken from {self.clickhouse_path_dir}")
            shutil.copytree(self.clickhouse_path_dir, db_dir)
            logging.debug(
                f"Database copied from {self.clickhouse_path_dir} to {db_dir}"
            )
        else:
            os.mkdir(db_dir)

        logs_dir = p.abspath(p.join(self.path, "logs"))
        logging.debug(f"Setup logs dir {logs_dir}")
        os.mkdir(logs_dir)
        self.logs_dir = logs_dir

        depends_on = []

        if self.with_mysql_client:
            depends_on.append(self.cluster.mysql_client_host)

        if self.with_mysql57:
            depends_on.append("mysql57")

        if self.with_mysql8:
            depends_on.append("mysql80")

        if self.with_mysql_cluster:
            depends_on.append("mysql80")
            depends_on.append("mysql2")
            depends_on.append("mysql3")
            depends_on.append("mysql4")

        if self.with_postgres_cluster:
            depends_on.append("postgres2")
            depends_on.append("postgres3")
            depends_on.append("postgres4")

        if self.with_kafka:
            depends_on.append("kafka1")
            depends_on.append("schema-registry")

        if self.with_kafka_sasl:
            depends_on.append("kafka_sasl")

        if self.with_kerberized_kafka:
            depends_on.append("kerberized_kafka1")

        if self.with_kerberos_kdc:
            depends_on.append("kerberoskdc")

        if self.with_ldap:
            depends_on.append("openldap")

        if self.with_rabbitmq:
            depends_on.append("rabbitmq1")

        if self.with_nats:
            depends_on.append("nats1")

        if self.with_zookeeper:
            depends_on += list(ZOOKEEPER_CONTAINERS)

        if self.with_minio:
            depends_on.append("minio1")

        if self.with_azurite:
            depends_on.append("azurite1")

        # In case the environment variables are exclusive, we don't want it to be in the cluster's env file.
        # Instead, a separate env file will be created for the instance and needs to be filled with cluster's env variables.
        if self.instance_env_variables is True:
            # Create a dictionary containing cluster & instance env variables.
            # Instance env variables will override cluster's.
            temp_env_variables = self.cluster.env_variables.copy()
            temp_env_variables.update(self.env_variables)
            self.env_variables = temp_env_variables
        else:
            self.cluster.env_variables.update(self.env_variables)

        odbc_ini_path = ""
        if self.odbc_ini_path:
            self._create_odbc_config_file()
            odbc_ini_path = "- " + self.odbc_ini_path

        entrypoint_cmd = self.clickhouse_start_command

        if self.stay_alive:
            entrypoint_cmd = self.clickhouse_stay_alive_command
        else:
            entrypoint_cmd = (
                "["
                + ", ".join(map(lambda x: '"' + x + '"', entrypoint_cmd.split()))
                + "]"
            )

        logging.debug("Entrypoint cmd: {}".format(entrypoint_cmd))

        networks = app_net = ipv4_address = ipv6_address = net_aliases = net_alias1 = ""
        if (
            self.ipv4_address is not None
            or self.ipv6_address is not None
            or self.hostname != self.name
        ):
            networks = "networks:"
            app_net = "default:"
            if self.ipv4_address is not None:
                ipv4_address = "ipv4_address: " + self.ipv4_address
            if self.ipv6_address is not None:
                ipv6_address = "ipv6_address: " + self.ipv6_address
            if self.hostname != self.name:
                net_aliases = "aliases:"
                net_alias1 = "- " + self.hostname

        if not self.with_installed_binary:
            binary_volume = "- " + self.server_bin_path + ":/usr/bin/clickhouse"
        else:
            binary_volume = "- " + self.server_bin_path + ":/usr/share/clickhouse_fresh"

        external_dirs_volumes = ""
        if self.external_dirs:
            for external_dir in self.external_dirs:
                external_dir_abs_path = p.abspath(
                    p.join(self.cluster.instances_dir, external_dir.lstrip("/"))
                )
                logging.info(f"external_dir_abs_path={external_dir_abs_path}")
                os.makedirs(external_dir_abs_path, exist_ok=True)
                external_dirs_volumes += (
                    "- " + external_dir_abs_path + ":" + external_dir + "\n"
                )

        # The current implementation of `self.env_variables` is not exclusive. Meaning the variables
        # are shared with all nodes within the same cluster, even if it is specified for a single node.
        # In order not to break the existing tests, the `self.instance_env_variables` option was added as a workaround.
        # IMHO, it would be better to make `self.env_variables` exclusive by default and remove the `self.instance_env_variables` option.
        if self.instance_env_variables:
            self.env_file = p.abspath(p.join(self.path, ".env"))
            _create_env_file(self.env_file, self.env_variables)

        with open(self.docker_compose_path, "w") as docker_compose:
            docker_compose.write(
                DOCKER_COMPOSE_TEMPLATE.format(
                    image=self.image,
                    tag=self.tag,
                    name=self.name,
                    hostname=self.hostname,
                    binary_volume=binary_volume,
                    instance_config_dir=instance_config_dir,
                    config_d_dir=self.config_d_dir,
                    db_dir=db_dir,
                    external_dirs_volumes=external_dirs_volumes,
                    tmpfs=str(self.tmpfs),
                    mem_limit=self.mem_limit,
                    logs_dir=logs_dir,
                    depends_on=str(depends_on),
                    user=os.getuid(),
                    env_file=self.env_file,
                    odbc_ini_path=odbc_ini_path,
                    keytab_path=self.keytab_path,
                    krb5_conf=self.krb5_conf,
                    entrypoint_cmd=entrypoint_cmd,
                    networks=networks,
                    app_net=app_net,
                    ipv4_address=ipv4_address,
                    ipv6_address=ipv6_address,
                    net_aliases=net_aliases,
                    net_alias1=net_alias1,
                    init_flag="true" if self.docker_init_flag else "false",
                )
            )

    def wait_for_path_exists(self, path, seconds):
        while seconds > 0:
            seconds -= 1
            if self.path_exists(path):
                return
            time.sleep(1)

    def get_backuped_s3_objects(self, disk, backup_name):
        path = f"/var/lib/clickhouse/disks/{disk}/shadow/{backup_name}/store"
        self.wait_for_path_exists(path, 10)
        return self.get_s3_objects(path)

    def get_s3_objects(self, path):
        command = [
            "find",
            path,
            "-type",
            "f",
            "-exec",
            "grep",
            "-o",
            "r[01]\\{64\\}-file-[[:lower:]]\\{32\\}",
            "{}",
            ";",
        ]

        return self.exec_in_container(command).split("\n")

    def get_s3_data_objects(self, path):
        command = [
            "find",
            path,
            "-type",
            "f",
            "-name",
            "*.bin",
            "-exec",
            "grep",
            "-o",
            "r[01]\\{64\\}-file-[[:lower:]]\\{32\\}",
            "{}",
            ";",
        ]
        return self.exec_in_container(command).split("\n")

    def get_table_objects(self, table, database=None):
        objects = []
        database_query = ""
        if database:
            database_query = f"AND database='{database}'"
        data_paths = self.query(
            f"""
            SELECT arrayJoin(data_paths)
            FROM system.tables
            WHERE name='{table}'
            {database_query}
            """
        )
        paths = data_paths.split("\n")
        for path in paths:
            if path:
                objects = objects + self.get_s3_data_objects(path)
        return objects

    def create_format_schema(self, file_name, content):
        self.exec_in_container(
            [
                "bash",
                "-c",
                "echo '{}' > {}".format(
                    content, "/var/lib/clickhouse/format_schemas/" + file_name
                ),
            ]
        )


class ClickHouseKiller(object):
    def __init__(self, clickhouse_node):
        self.clickhouse_node = clickhouse_node

    def __enter__(self):
        self.clickhouse_node.stop_clickhouse(kill=True)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.clickhouse_node.start_clickhouse()


@cache
def is_arm():
    return any(arch in platform.processor().lower() for arch in ("arm, aarch"))
