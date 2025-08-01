#pragma once

#include <DataTypes/IDataType.h>
#include <optional>


namespace DB
{

/** Tuple data type.
  * Used as an intermediate result when evaluating expressions.
  * Also can be used as a column - the result of the query execution.
  *
  * Tuple elements can have names.
  * If an element is unnamed, it will have automatically assigned name like '1', '2', '3' corresponding to its position.
  * Manually assigned names must not begin with digit. Names must be unique.
  *
  * All tuples with same size and types of elements are equivalent for expressions, regardless to names of elements.
  */
class DataTypeTuple final : public IDataType
{
private:
    DataTypes elems;
    Strings names;
    bool has_explicit_names;

public:
    static constexpr bool is_parametric = true;

    explicit DataTypeTuple(const DataTypes & elems);
    DataTypeTuple(const DataTypes & elems, const Strings & names);

    TypeIndex getTypeId() const override { return TypeIndex::Tuple; }
    std::string doGetName() const override;
    std::string doGetPrettyName(size_t indent) const override;
    const char * getFamilyName() const override { return "Tuple"; }

    bool canBeInsideNullable() const override { return false; }
    bool supportsSparseSerialization() const override { return true; }
    bool canBeInsideSparseColumns() const override { return false; }

    MutableColumnPtr createColumn() const override;
    MutableColumnPtr createColumn(const ISerialization & serialization) const override;

    Field getDefault() const override;
    void insertDefaultInto(IColumn & column) const override;

    bool equals(const IDataType & rhs) const override;

    bool isParametric() const override { return true; }
    bool haveSubtypes() const override { return !elems.empty(); }
    bool isComparable() const override;
    bool textCanContainOnlyValidUTF8() const override;
    bool haveMaximumSizeOfValue() const override;
    bool hasDynamicSubcolumnsDeprecated() const override;
    size_t getMaximumSizeOfValueInMemory() const override;
    size_t getSizeOfValueInMemory() const override;

    SerializationPtr doGetDefaultSerialization() const override;
    SerializationPtr getSerialization(const SerializationInfo & info) const override;
    MutableSerializationInfoPtr createSerializationInfo(const SerializationInfoSettings & settings) const override;
    SerializationInfoPtr getSerializationInfo(const IColumn & column) const override;

    DataTypePtr getNormalizedType() const override;
    const DataTypePtr & getElement(size_t i) const { return elems[i]; }
    const DataTypes & getElements() const { return elems; }
    const Strings & getElementNames() const { return names; }

    size_t getPositionByName(const String & name, bool case_insensitive = false) const;
    std::optional<size_t> tryGetPositionByName(const String & name, bool case_insensitive = false) const;
    String getNameByPosition(size_t i) const;

    bool hasExplicitNames() const { return has_explicit_names; }

    void updateHashImpl(SipHash & hash) const override;

    void forEachChild(const ChildCallback & callback) const override;
};

}
