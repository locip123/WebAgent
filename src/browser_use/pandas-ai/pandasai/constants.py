"""Constants required by the embedded schema and LLM core."""

PANDABI_SETUP_MESSAGE = 'An explicit LLM adapter is required.'

LOCAL_SOURCE_TYPES = ['csv', 'parquet']
REMOTE_SOURCE_TYPES = [
    'mysql',
    'postgres',
    'cockroachdb',
    'sqlserver',
    'data',
    'yahoo_finance',
    'bigquery',
    'snowflake',
    'databricks',
    'oracle',
]
VALID_COLUMN_TYPES = ['string', 'integer', 'float', 'datetime', 'boolean']
VALID_TRANSFORMATION_TYPES = [
    'anonymize',
    'convert_timezone',
    'to_lowercase',
    'to_uppercase',
    'strip',
    'round_numbers',
    'scale',
    'format_date',
    'to_numeric',
    'to_datetime',
    'fill_na',
    'replace',
    'extract',
    'truncate',
    'pad',
    'clip',
    'bin',
    'normalize',
    'standardize',
    'map_values',
    'rename',
    'encode_categorical',
    'validate_email',
    'validate_date_range',
    'normalize_phone',
    'remove_duplicates',
    'validate_foreign_key',
    'ensure_positive',
    'standardize_categories',
]
