"""Minimal PandasAI DataFrame metadata/serialization surface."""

from __future__ import annotations

import hashlib
from typing import Any, Optional

import pandas as pd

from pandasai.constants import LOCAL_SOURCE_TYPES
from pandasai.data_loader.semantic_layer_schema import Column, SemanticLayerSchema, Source
from pandasai.helpers.dataframe_serializer import DataframeSerializer


class DataFrame(pd.DataFrame):
    _metadata = ['_column_hash', '_table_name', 'schema']

    def __init__(
        self,
        data: Any = None,
        index: Any = None,
        columns: Any = None,
        dtype: Any = None,
        copy: bool | None = None,
        **kwargs: Any,
    ) -> None:
        schema: SemanticLayerSchema | None = kwargs.pop('schema', None)
        table_name: str | None = kwargs.pop('_table_name', None)
        super().__init__(data=data, index=index, columns=columns, dtype=dtype, copy=copy)
        if table_name:
            self._table_name = table_name
        self._column_hash = self._calculate_column_hash()
        self.schema = schema or self.get_default_schema(self)

    def _calculate_column_hash(self) -> str:
        column_string = ','.join(str(value) for value in self.columns)
        return hashlib.sha256(column_string.encode()).hexdigest()[:16]

    @property
    def rows_count(self) -> int:
        return len(self)

    @property
    def columns_count(self) -> int:
        return len(self.columns)

    def get_dialect(self) -> str:
        source = self.schema.source
        if source and source.type not in LOCAL_SOURCE_TYPES:
            return source.type
        return 'duckdb'

    def serialize_dataframe(self) -> str:
        return DataframeSerializer.serialize(self, self.get_dialect())

    @staticmethod
    def get_column_type(column_dtype: Any) -> Optional[str]:
        if pd.api.types.is_string_dtype(column_dtype):
            return 'string'
        if pd.api.types.is_integer_dtype(column_dtype):
            return 'integer'
        if pd.api.types.is_float_dtype(column_dtype):
            return 'float'
        if pd.api.types.is_datetime64_any_dtype(column_dtype):
            return 'datetime'
        if pd.api.types.is_bool_dtype(column_dtype):
            return 'boolean'
        return None

    @classmethod
    def get_default_schema(cls, dataframe: DataFrame) -> SemanticLayerSchema:
        columns = [
            Column(name=str(name), type=cls.get_column_type(dtype))
            for name, dtype in dataframe.dtypes.items()
        ]
        table_name = getattr(dataframe, '_table_name', f'table_{dataframe._column_hash}')
        return SemanticLayerSchema(
            name=table_name,
            source=Source(type='parquet', path='data.parquet'),
            columns=columns,
        )
