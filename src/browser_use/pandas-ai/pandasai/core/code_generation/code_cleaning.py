"""Clean generated SQL-wrapper code without plotting or execution helpers."""

import ast
import re

import astor

from pandasai.agent.state import AgentState
from pandasai.exceptions import MaliciousQueryError
from pandasai.query_builders.sql_parser import SQLParser


class CodeCleaner:
    def __init__(self, context: AgentState):
        self.context = context

    def _clean_sql_query(self, sql_query: str) -> str:
        sql_query = sql_query.rstrip(';')
        dialect = self.context.dfs[0].get_dialect()
        table_names = SQLParser.extract_table_names(sql_query, dialect)
        allowed = {df.schema.name: df.schema.name for df in self.context.dfs}
        for table_name in table_names:
            if table_name not in allowed:
                raise MaliciousQueryError(f'Query uses unauthorized table: {table_name}.')
            sql_query = re.sub(r'\b' + re.escape(table_name) + r'\b', allowed[table_name], sql_query)
        return sql_query

    def _validate_sql_call(self, node: ast.AST) -> ast.AST:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            return node
        call = node.value
        if (
            isinstance(call.func, ast.Name)
            and call.func.id == 'execute_sql_query'
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            call.args[0].value = self._clean_sql_query(call.args[0].value)
        return node

    def clean_code(self, code: str) -> str:
        tree = ast.parse(code)
        cleaned = ast.Module(body=[self._validate_sql_call(node) for node in tree.body], type_ignores=[])
        return astor.to_source(cleaned, pretty_source=lambda value: ''.join(value)).strip()
