import google.api_core.exceptions
import random
import string
import re

from google.cloud import storage
from google.cloud import bigquery

from dateutil.parser import parse
from datetime import datetime

from parquet2bigquery.logs import configure_logging

from multiprocessing import Process, JoinableQueue, Lock


log = configure_logging()

# defaults
DEFAULT_DATASET = 'telemetry'
DEFAULT_TMP_DATASET = 'tmp'

ignore_patterns = [
    r'.*/$',  # dirs
    r'.*/_[^=/]*/',  # temp dirs
    r'.*/_[^/]*$',  # temp files
    r'.*/[^/]*\$folder\$/?',  # metadata dirs and files
    r'.*\/.spark-staging.*$',  # spark staging dirs
]


class ParquetFormatError(Exception):
    pass


class GCError(Exception):
    pass


class GCWarning(Exception):
    pass


def get_date_format(date):

    date_formats = [
        '%Y%m%d',
        '%Y-%m-%d'
     ]

    for format in date_formats:
        try:
            datetime.strptime(date, format)
            log.info("date format {} detected".format(format))
            return format
        except ValueError:
            continue

    log.error('Date format not detected, exit')
    # raise ProcessingError('date format could not be determined')


def tmp_prefix(size=5):
    char_set = string.ascii_lowercase + string.digits
    return ''.join(random.choice(char_set) for x in range(size))


def ignore_key(key, exclude_regex=None):
    if exclude_regex is None:
        exclude_regex = []
    return any([re.match(pat, key) for pat in ignore_patterns + exclude_regex])


def normalize_table_id(table_name):
    return table_name.replace("-", "_").lower()


def bq_modes(elem):
    REP_CONVERSIONS = {
            'required': 'REQUIRED',
            'optional': 'NULLABLE',
            'repeated': 'REPEATED',

    }
    return REP_CONVERSIONS[elem]


def bq_legacy_types(elem):

    CONVERSIONS = {
        'boolean': 'BOOLEAN',
        'byte_array': 'STRING',
        'double': 'FLOAT',
        'fixed_len_byte_array': 'NUMERIC',
        'float': 'FLOAT',
        'group': 'RECORD',
        'int32': 'INTEGER',
        'int64': 'INTEGER',
        'int96': 'TIMESTAMP',
        'list': 'RECORD',
        'map': 'RECORD',
    }

    return CONVERSIONS[elem]


def _get_object_key_metadata(object, prefix):

    meta = {
        'partitions': []
    }
    o = object.split('/')

    meta['table_id'] = normalize_table_id('_'.join([o[0], o[1]]))

    meta['date_partition'] = o[2].split('=')
    meta['date_format'] = get_date_format(meta['date_partition'][1])
    meta['date_partition'][1] = parse(meta['date_partition'][1]).strftime('%Y-%m-%d')
    # try to get partition information
    for dir in o[3:]:
        if '=' in dir:
            meta['partitions'].append(dir.split('='))

    return meta


def get_partitioning_fields(prefix):
    return re.findall("([^=/]+)=[^=/]+", prefix)


def create_bq_table(table_id, dataset, schema=None, partition_field=None):

    from google.cloud.bigquery.table import TimePartitioning
    from google.cloud.bigquery.table import TimePartitioningType

    client = bigquery.Client()
    dataset_ref = client.dataset(dataset)
    table_ref = dataset_ref.table(table_id)
    table = bigquery.Table(table_ref, schema=schema)

    if partition_field:
        _tp = TimePartitioning(type_=TimePartitioningType.DAY,
                               field=partition_field)
        table.time_partitioning = _tp

    table = client.create_table(table)

    assert table.table_id == table_id
    log.info('%s: table created.' % table_id)


def get_bq_table_schema(table_id, dataset):

    client = bigquery.Client()
    dataset_ref = client.dataset(dataset)
    table_ref = dataset_ref.table(table_id)

    table = client.get_table(table_ref)

    return table.schema


def update_bq_table_schema(table_id, schema_additions, dataset):

    client = bigquery.Client()
    dataset_ref = client.dataset(dataset)
    table_ref = dataset_ref.table(table_id)

    table = client.get_table(table_ref)
    new_schema = table.schema[:]

    for field in schema_additions:
        new_schema.append(field)

    table.schema = new_schema
    table = client.update_table(table, ['schema'])
    log.info('%s: BigQuery table schema updated.' % table_id)


def generate_bq_schema(table_id, dataset, date_partition_field=None,
                       partitions=None):
    p_fields = []

    schema = get_bq_table_schema(table_id, dataset)

    if date_partition_field:
        p_fields.append(bigquery.SchemaField(date_partition_field, 'DATE',
                                             mode='REQUIRED'))
    # we want to add the partitions to the schema
    for part, _ in partitions:
        p_fields.append(bigquery.SchemaField(part, 'STRING',
                                             mode='REQUIRED'))

    return p_fields + schema


def load_parquet_to_bq(bucket, object, table_id, dataset, schema=None,
                       partition=None):
    client = bigquery.Client()
    dataset_ref = client.dataset(dataset)
    job_config = bigquery.LoadJobConfig()
    job_config.source_format = bigquery.SourceFormat.PARQUET
    if schema:
        job_config.schema = schema
    job_config.schema_update_options = [
        bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION,
        bigquery.SchemaUpdateOption.ALLOW_FIELD_RELAXATION
    ]

    uri = 'gs://%s/%s' % (bucket, object)

    if partition:
        table_id = '%s$%s' % (table_id, partition)

    load_job = client.load_table_from_uri(
        uri,
        dataset_ref.table(table_id),
        job_config=job_config)
    assert load_job.job_type == 'load'
    load_job.result()
    log.info('%s: Parquet file %s loaded into BigQuery.' % (table_id, object))


# we need to check and see what is changing
def _compare_columns(col1, col2):

    a = []
    if isinstance(col1, tuple):
        for i in range(len(col1)):
            a.append(_compare_columns(col1[i], col2[i]))

    if isinstance(col1, google.cloud.bigquery.schema.SchemaField):
        if col1.fields and col2.fields:
            a.append(_compare_columns(col1.fields, col2.fields))

        # if mode is changing from required to nullable ignore
        if(col1.mode == 'REQUIRED' and col2.mode == 'NULLABLE'):
            return None


def get_schema_diff(schema1, schema2):
    s = []
    diff = set(schema2) - set(schema1)

    for d in diff:
        found = False
        for o in schema1:
            if d.name == o.name:
                found = True
                val = (_compare_columns(d, o))
                if val:
                    s.append(d)
                    break
        if not found:
            s.append(d)
    return(s)


def construct_select_query(table_id, date_partition, partitions=None,
                           dataset=DEFAULT_TMP_DATASET):
    s_items = ['SELECT *']

    s_items.append("CAST('{0}' AS DATE) as {1}".format(date_partition[1],
                                                       date_partition[0]))

    part_as = "'{1}' as {0}"
    for partition in partitions:
        s_items.append(part_as.format(*partition))

    _tmp_s = ','.join(s_items)

    query = """
    {0}
    FROM {1}.{2}
    """.format(_tmp_s, dataset, table_id)

    return query


def check_bq_partition_exists(table_id, date_partition, dataset,
                              partitions=None):
    client = bigquery.Client()
    job_config = bigquery.QueryJobConfig()
    job_config.use_query_cache = False

    count = 0
    s_items = []
    part_as = "{0} = '{1}'"

    s_items.append(part_as.format(date_partition[0], date_partition[1]))

    for partition in partitions:
        s_items.append(part_as.format(*partition))

    _tmp_s = ' AND '.join(s_items)

    query = """
    SELECT 1
    FROM {0}.{1}
    WHERE {2}
    LIMIT 10
    """.format(dataset, table_id, _tmp_s)

    query_job = client.query(query, job_config=job_config)
    results = query_job.result()
    for row in results:
        count += 1

    if count == 0:
        return False
    else:
        return True


def load_bq_query_to_table(query, table_id, dataset):
    job_config = bigquery.QueryJobConfig()
    client = bigquery.Client()
    dataset_ref = client.dataset(dataset)
    table_ref = dataset_ref.table(table_id)
    job_config.destination = table_ref
    job_config.write_disposition = bigquery.job.WriteDisposition.WRITE_APPEND

    query_job = client.query(query, job_config=job_config)
    query_job.result()
    log.info('{}: query results loaded'.format(table_id))


def check_bq_table_exists(table_id, dataset):
    client = bigquery.Client()
    dataset_ref = client.dataset(dataset)
    table_ref = dataset_ref.table(table_id)
    try:
        table = client.get_table(table_ref)
        if table:
            log.info('{}: table exists.'.format(table_id))
            return True
    except google.api_core.exceptions.NotFound:
        log.info('{}: table does not exist.'.format(table_id))
        return False


def delete_bq_table(table_id, dataset=DEFAULT_TMP_DATASET):
    client = bigquery.Client()
    dataset_ref = client.dataset(dataset)
    table_ref = dataset_ref.table(table_id)
    try:
        client.delete_table(table_ref)
        log.info('{}: table deleted.'.format(table_id))
    except google.api_core.exceptions.NotFound:
        pass


def list_blobs_with_prefix(bucket_name, prefix, delimiter=None):
    storage_client = storage.Client()
    bucket = storage_client.get_bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=prefix, delimiter=delimiter)

    objects = []

    for blob in blobs:
        if not ignore_key(blob.name):
            objects.append(blob.name)

    return objects


def get_latest_object(bucket_name, prefix, delimiter=None):
    storage_client = storage.Client()
    bucket = storage_client.get_bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=prefix, delimiter=delimiter)

    latest_objects = {}
    latest_objects_tmp = {}

    for blob in blobs:
        if not ignore_key(blob.name):
            dir = '/'.join(blob.name.split('/')[0:-1])
            if latest_objects_tmp.get(dir):
                if latest_objects_tmp[dir] < blob.updated:
                    latest_objects_tmp[dir] = blob.updated
                    latest_objects[dir] = blob.name
            else:
                latest_objects_tmp[dir] = blob.updated
                latest_objects[dir] = blob.name

    return latest_objects


def create_primary_bq_table(table_id, new_schema, date_partition_field,
                            dataset):
    # check to see if the table exists if not create it
    if not check_bq_table_exists(table_id, dataset):
        try:
            create_bq_table(table_id, dataset, new_schema,
                            date_partition_field)
        except google.api_core.exceptions.Conflict:
            pass


def run(bucket_name, object, dest_dataset, dir=None, lock=None, alias=None):
    if ignore_key(object):
        log.warning('Ignoring {}'.format(object))
        return

    meta = _get_object_key_metadata(object)

    if alias:
        table_id = alias
    else:
        table_id = meta['table_id']
    date_partition_field = meta['date_partition'][0]
    date_partition_value = meta['date_partition'][1]

    table_id_tmp = normalize_table_id('_'.join([meta['table_id'],
                                      date_partition_value,
                                      tmp_prefix()]))

    query = construct_select_query(table_id_tmp, meta['date_partition'],
                                   partitions=meta['partitions'])

    if dir:
        obj = '{}/*'.format(dir)
        if object.endswith('parquet'):
            obj += 'parquet'
        elif object.endswith('internal'):
            obj += 'internal'
    else:
        obj = object

    # we want to create the temp table and load the data
    try:
        create_bq_table(table_id_tmp, DEFAULT_TMP_DATASET)
        load_parquet_to_bq(bucket_name, obj, table_id_tmp,
                           DEFAULT_TMP_DATASET)
    except (google.api_core.exceptions.InternalServerError,
            google.api_core.exceptions.ServiceUnavailable):
        delete_bq_table(table_id_tmp, dataset=DEFAULT_TMP_DATASET)
        log.exception('%s: BigQuery Retryable Error' % table_id)
        raise GCWarning('BigQuery Retryable Error')

    # data is now loaded, we want to grab the schema of the table
    try:
        new_schema = generate_bq_schema(table_id_tmp,
                                        DEFAULT_TMP_DATASET,
                                        date_partition_field,
                                        meta['partitions'])
    except (google.api_core.exceptions.InternalServerError,
            google.api_core.exceptions.ServiceUnavailable):
        log.exception('%s: GCS Retryable Error' % table_id)
        raise GCWarning('GCS Retryable Error')

    if lock:
        lock.acquire()
        try:
            create_primary_bq_table(table_id, new_schema,
                                    date_partition_field, dest_dataset)
        finally:
            lock.release()
    else:
        create_primary_bq_table(table_id, new_schema,
                                date_partition_field, dest_dataset)

    current_schema = get_bq_table_schema(table_id, dest_dataset)
    schema_additions = get_schema_diff(current_schema, new_schema)

    if len(schema_additions) > 0:
        update_bq_table_schema(table_id, schema_additions, dest_dataset)

    log.info('%s: loading %s/%s to BigQuery table %s' % (table_id, bucket_name,
                                                         obj, table_id_tmp))
    try:
        load_bq_query_to_table(query, table_id, dest_dataset)
    except (google.api_core.exceptions.InternalServerError,
            google.api_core.exceptions.ServiceUnavailable):
        log.exception('%s: BigQuery Retryable Error' % table_id)
        raise GCWarning('BigQuery Retryable Error')
    finally:
        delete_bq_table(table_id_tmp)


def get_bq_table_partitions(table_id, date_partition, dataset, partitions=[]):
    client = bigquery.Client()

    s_items = []
    reconstruct_paths = []

    s_items.append(date_partition[0])

    for partition in partitions:
        s_items.append(partition[0])

    _tmp_s = ','.join(s_items)

    query = """
    SELECT {2}
    FROM {0}.{1}
    GROUP BY {2}
    """.format(dataset, table_id, _tmp_s)

    query_job = client.query(query)
    results = query_job.result()

    for row in results:
        tmp_path = []
        for item in s_items:
            tmp_path.append('{}={}'.format(item, row[item]))
        reconstruct_paths.append('/'.join(tmp_path))

    return reconstruct_paths


def remove_loaded_objects(objects, dataset):
    initial_object_tmp = list(objects)[0]
    path_prefix = initial_object_tmp.split('/')[:2]
    meta = _get_object_key_metadata(initial_object_tmp)

    if not check_bq_table_exists(meta['table_id'], dataset):
        return objects

    object_paths = get_bq_table_partitions(meta['table_id'],
                                           meta['date_partition'],
                                           dataset,
                                           meta['partitions'])

    for path in object_paths:
        spath = path.split('/')
        date_value = parse(spath[0].split('=')[1]).strftime(meta['date_format'])
        spath[0] = '='.join([meta['date_partition'][0]] + [date_value])
        key = '/'.join(path_prefix + spath)
        try:
            del objects[key]
            log.debug('key {} already loaded into BigQuery'.format(key))
        except KeyError:
            continue

    return objects


# we want to bulk load by a partition that makes sense
def bulk(bucket_name, prefix, concurrency, glob_load, resume_load,
         dest_dataset=None, alias=None):

    if dest_dataset:
        _dest_dataset = dest_dataset
    else:
        _dest_dataset = DEFAULT_DATASET

    log.info('main_process: dataset set to {}'.format(_dest_dataset))

    q = JoinableQueue()
    lock = Lock()

    if glob_load:
        log.info('main_process: loading via glob method')
        objects = get_latest_object(bucket_name, prefix)
        if resume_load:
            objects = remove_loaded_objects(objects, _dest_dataset)

        for dir, object in objects.items():
            q.put((bucket_name, dir, object))
    else:
        log.info('main_process: loading via non-glob method')
        objects = list_blobs_with_prefix(bucket_name, prefix)
        for object in objects:
            q.put((bucket_name, None, object))

    for c in range(concurrency):
        p = Process(target=_bulk_run, args=(c, lock, q, _dest_dataset, alias,))
        p.daemon = True
        p.start()

    log.info('main_process: {} total tasks in queue'.format(q.qsize()))

    q.join()

    for c in range(concurrency):
        q.put(None)

    log.info('main_process: done')


def _bulk_run(process_id, lock, q, dest_dataset, alias):
    log.info('Process-{}: started'.format(process_id))

    for item in iter(q.get, None):
        bucket_name, dir, object = item
        try:
            o = object if dir is None else dir
            log.info('Process-{}: running {}'.format(process_id, o))
            run(bucket_name, object, dest_dataset, dir=dir,
                lock=lock, alias=alias)
        except GCWarning:
            q.put(item)
            log.warn('Process-{}: Re-queueed {}'
                     'due to warning'.format(process_id,
                                             object))
        finally:
            q.task_done()
            log.info('Process-{}: {} tasks left in queue'.format(process_id,
                                                                 q.qsize()))
    q.task_done()
    log.info('Process-{}: done'.format(process_id))
