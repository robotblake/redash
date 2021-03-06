import json
import os

try:
    import botocore.session
    direct_enabled = True
except ImportError:
    direct_enabled = False

from redash.query_runner import BaseQueryRunner, register
from redash.utils import JSONEncoder

PROXY_URL = os.environ.get('ATHENA_PROXY_URL')

class Athena(BaseQueryRunner):
    noop_query = 'SELECT 1'

    @classmethod
    def name(cls):
        return "Amazon Athena (via JDBC)"

    @classmethod
    def configuration_schema(cls):
        return {
            'type': 'object',
            'properties': {
                'region': {
                    'type': 'string',
                    'title': 'AWS Region'
                },
                'aws_access_key': {
                    'type': 'string',
                    'title': 'AWS Access Key'
                },
                'aws_secret_key': {
                    'type': 'string',
                    'title': 'AWS Secret Key'
                },
                's3_staging_dir': {
                    'type': 'string',
                    'title': 'S3 Staging Path'
                }
            },
            'required': ['region', 'aws_access_key', 'aws_secret_key', 's3_staging_dir'],
            'secret': ['aws_secret_key']
        }


    def get_schema(self, get_stats=False):
        schema = {}
        query = """
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
        """

        results, error = self.run_query(query, None)

        if error is not None:
            raise Exception("Failed getting schema.")

        results = json.loads(results)

        for row in results['rows']:
            table_name = '{}.{}'.format(row['table_schema'], row['table_name'])

            if table_name not in schema:
                schema[table_name] = {'name': table_name, 'columns': []}

            schema[table_name]['columns'].append(row['column_name'])

        return schema.values()

    def run_query(self, query, user):
        try:
            data = {
                'athenaUrl': 'jdbc:awsathena://athena.{}.amazonaws.com:443/'.format(self.configuration['region'].lower()),
                'awsAccessKey': self.configuration['aws_access_key'],
                'awsSecretKey': self.configuration['aws_secret_key'],
                's3StagingDir': self.configuration['s3_staging_dir'],
                'query': query
            }

            response = requests.post(PROXY_URL, json=data)
            response.raise_for_status()

            json_data = response.content.strip()
            error = None

            return json_data, error
        except requests.RequestException as e:
            if e.response.status_code == 400:
                return None, response.content

            return None, str(e)
        except KeyboardInterrupt:
            error = "Query cancelled by user."
            json_data = None

        return json_data, error

register(Athena)

class AthenaDirect(BaseQueryRunner):
    noop_query = 'SELECT 1'

    @classmethod
    def name(cls):
        return "Amazon Athena (direct)"

    @classmethod
    def enabled(cls):
        return direct_enabled

    @classmethod
    def configuration_schema(cls):
        return {
            'type': 'object',
            'properties': {
                'region': {
                    'type': 'string',
                    'title': 'AWS Region'
                },
                'aws_access_key': {
                    'type': 'string',
                    'title': 'AWS Access Key'
                },
                'aws_secret_key': {
                    'type': 'string',
                    'title': 'AWS Secret Key'
                },
                's3_staging_dir': {
                    'type': 'string',
                    'title': 'S3 Staging Path'
                },
                'database': {
                    'type': 'string',
                    'default': 'default'
                }
            },
            'required': ['region', 'aws_access_key', 'aws_secret_key', 's3_staging_dir'],
            'secret': ['aws_secret_key']
        }

    def _get_client(self):
        BASE_DIR = os.path.dirname(os.path.realpath(__file__))
        session = botocore.session.get_session()
        session.set_credentials(self.configuration['aws_access_key'], self.configuration['aws_secret_key'])
        session.set_config_variable('region', self.configuration['region'])
        session.set_config_variable('data_path', os.path.join(BASE_DIR, 'athena_models'))
        return session.create_client('athena')

    def get_schema(self, get_stats=False):
        client = self._get_client()
        schemas = []
        schema_names = [n['Name'] for n in client.get_namespaces()['NamespaceList']]
        for schema_name in schema_names:
            tables = client.get_tables(NamespaceName=schema_name)['TableList']
            for table in tables:
                schemas.append({'name': '%s.%s' % (schema_name, table['Name']),
                                'columns': [col['Name'] for col in table['StorageDescriptor']['Columns']]})
        return schemas

    def run_query(self, query, user):
        client = self._get_client()
        response = client.run_query(
            Query=query,
            OutputLocation=self.configuration['s3_staging_dir'],
            QueryExecutionContext={'Database': self.configuration['database']})
        waiter = client.get_waiter('query_completed')
        waiter.wait(QueryExecutionId=response['QueryExecutionId'])

        paginator = client.get_paginator('get_query_results')
        iterator = paginator.paginate(QueryExecutionId=response['QueryExecutionId'])
        # this API isn't documented so let's do a consistency check
        column_info_set = set()
        column_info = None
        rows = []
        for result in iterator:
            column_info = result['ResultSet']['ColumnInfos']
            column_info_set.add(json.dumps(result['ResultSet']['ColumnInfos']))
            assert len(column_info_set) == 1, "Don't know what to do with inconsistent column info"
            rows.extend(result['ResultSet']['ResultRows'])
        cnames = [c['Name'] for c in column_info]
        data = {'columns':
                [{
                    'name': name,
                    'friendly_name': name,
                    'type': 'string', # XXX map athena types to redash types
                } for name in cnames],
                'rows':
                [{name: row['Data'][i] for (i, name) in enumerate(cnames)}
                 for row in rows[1:]]
        }

        return json.dumps(data, cls=JSONEncoder), None

register(AthenaDirect)
