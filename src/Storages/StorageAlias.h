#pragma once

#include <Storages/IStorage.h>

namespace DB
{

/* An alias for another table. */
class StorageAlias final : public IStorage
{
public:
    StorageAlias(const StorageID & table_id_, const StorageID & ref_table_id_);
    std::string getName() const override { return "Alias"; }

    std::shared_ptr<IStorage> getReferenceTable(ContextPtr context) const;

    StorageInMemoryMetadata getInMemoryMetadata() const override;
    StorageMetadataPtr getInMemoryMetadataPtr() const override;
    void alter(const AlterCommands &, ContextPtr, AlterLockHolder &) override;
private:
    ContextPtr getContext() const;
    /// Store ddatabase.table or UUID of the reference table.
    /// ref_table_id.uuid is Nil, find the table by ref_table_id.database_name, ref_table_id.table_name;
    /// otherwise find the table by ref_table_id.uuid.
    StorageID ref_table_id;
};

}
