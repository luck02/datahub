import logging
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Tuple

from datahub.configuration.common import AllowDenyPattern, ConfigModel
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.source import Source, SourceReport, WorkUnit
from datahub.metadata.com.linkedin.pegasus2avro.common import AuditStamp
from datahub.metadata.com.linkedin.pegasus2avro.metadata.snapshot import DatasetSnapshot
from datahub.metadata.com.linkedin.pegasus2avro.mxe import MetadataChangeEvent
from datahub.metadata.com.linkedin.pegasus2avro.schema import (
    ArrayTypeClass,
    BooleanTypeClass,
    BytesTypeClass,
    EnumTypeClass,
    MySqlDDL,
    NullTypeClass,
    NumberTypeClass,
    SchemaField,
    SchemaFieldDataType,
    SchemaMetadata,
    StringTypeClass,
)

from datahub.metadata.com.linkedin.pegasus2avro.dataset import (
    Upstream,
    UpstreamClass,
    UpstreamLineage,
    UpstreamLineageClass,
    DatasetLineageType
)

from datahub.metadata.schema_classes import DatasetPropertiesClass
from datahub.ingestion.source.metadata_common import MetadataWorkUnit
from datahub.metadata.com.linkedin.pegasus2avro.mxe import MetadataChangeEvent
import json
from pprint import pprint
import re 

logger = logging.getLogger(__name__)


@dataclass
class DBTSourceReport(SourceReport):
    tables_scanned = 0
    filtered: List[str] = field(default_factory=list)

    def report_table_scanned(self, table_name: str) -> None:
        self.tables_scanned += 1

    def report_dropped(self, table_name: str) -> None:
        self.filtered.append(table_name)


class DBTManifestConfig(ConfigModel):
    manifest_path: str = None
    catalog_path: str = None

class DBTColumn():
    name: str
    comment: str
    index: int
    data_type: str

    def __repr__(self):
         fields = tuple("{}={}".format(k, v) for k, v in self.__dict__.items())
         return self.__class__.__name__ + str(tuple(sorted(fields))).replace("\'","")

class DBTNode():
    dbt_name: str
    database: str
    schema: str
    dbt_file_path: str
    node_type: str # source, model
    materialization: str # table, view, ephemeral
    relation_name: str
    columns: list[DBTColumn]
    upstream_urns: list[str]
    datahub_urn: str

    def __repr__(self):
         fields = tuple("{}={}".format(k, v) for k, v in self.__dict__.items())
         return self.__class__.__name__ + str(tuple(sorted(fields))).replace("\'","")


def get_columns(catalog_node) -> list[DBTColumn]:
    columns = []

    raw_columns = catalog_node['columns']

    for key in raw_columns:
        raw_column = raw_columns[key]

        dbtCol = DBTColumn() 
        dbtCol.comment = raw_column['comment']
        dbtCol.data_type = raw_column['type']
        dbtCol.index = raw_column['index']
        dbtCol.name = raw_column['name']
        columns.append(dbtCol)
    return columns 

def extract_dbt_entities(nodes, catalog, platform: str, environment: str) -> List[DBTNode]:
    dbt_entities = []

    for key in nodes:
        node = nodes[key]
        dbtNode = DBTNode()
        
        dbtNode.dbt_name = key
        dbtNode.node_type = node['resource_type']
        dbtNode.relation_name = node['relation_name']
        dbtNode.database = node['database']
        dbtNode.schema = node['schema']
        dbtNode.dbt_file_path = node['original_file_path']

        if 'materialized' in node['config'].keys():
            # It's a model
            dbtNode.materialization = node['config']['materialized']
            dbtNode.upstream_urns = get_upstreams(
                node['depends_on']['nodes'], 
                nodes,
                platform,
                environment
            )
        else:
            # It's a source 
            dbtNode.materialization = catalog[key]['metadata']['type']
            dbtNode.upstream_urns = []

        if dbtNode.materialization != 'ephemeral':
            dbtNode.columns = get_columns(catalog[dbtNode.dbt_name])
        else:
            dbtNode.columns = []

        dbtNode.datahub_urn = get_urn_from_dbtNode(dbtNode.database, dbtNode.schema, dbtNode.relation_name, platform, environment)
        
        dbt_entities.append(dbtNode)

    return dbt_entities


def loadManifestAndCatalog(manifest_path, catalog_path, platform, environment) -> list[DBTNode]:
    manifest_nodes = list[DBTNode]
    
    with open(manifest_path, "r") as manifest:
        with open(catalog_path, "r") as catalog: 
            dbt_manifest_json = json.load(manifest)
            dbt_catalog_json = json.load(catalog)

            manifest_nodes = dbt_manifest_json['nodes']
            manifest_sources = dbt_manifest_json['sources']

            all_manifest_entities = manifest_nodes | manifest_sources

            catalog_nodes = dbt_catalog_json['nodes']
            catalog_sources = dbt_catalog_json['sources']

            all_catalog_entities = catalog_nodes | catalog_sources

            logger.info('loading {} nodes, {} sources'.format(len(all_manifest_entities), len(all_catalog_entities)))
    
            nodes = extract_dbt_entities(all_manifest_entities, all_catalog_entities, platform, environment)

            return nodes

def get_urn_from_dbtNode(database: str, schema: str, relation_name: str, platform: str, env:str) -> str:
    db_fqn = f"{database}.{schema}.{relation_name}".replace('"', '')
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{db_fqn},{env})"

def get_custom_properties(node: DBTNode) -> List[dict[str,str]]:
    tags = {}
    tags['dbt_node_type'] = node.node_type
    tags['materialization'] = node.materialization
    tags['dbt_file_path'] = node.dbt_file_path
    return tags

def get_upstreams(upstreams: List[str], all_nodes, platform: str, environment: str) -> List[str]:
    upstream_urns = []

    for upstream in upstreams:
        upstream_node = all_nodes[upstream]

        upstream_urns.append(get_urn_from_dbtNode(
            upstream_node['database'],
            upstream_node['schema'],
            upstream_node['relation_name'],
            platform,
            environment
        ))

    return upstream_urns

def get_upstream_lineage(upstream_urns: List[str]) -> UpstreamLineage:
    ucl: List[uc] = []

    actor, sys_time = "urn:li:corpuser:dbt_executor", int(time.time()) * 1000

    for dep in upstream_urns:
        uc = UpstreamClass(
        dataset=dep, 
        auditStamp=AuditStamp(
            actor=actor,
            time=sys_time # replace with system timestamp etc.
        ),
        type="TRANSFORMED"
        )
        ucl.append(uc)

    ulc = UpstreamLineageClass(
        upstreams = ucl
    )

    return ulc

_field_type_mapping = {
    'boolean': BooleanTypeClass,
    'date': StringTypeClass, # Is there no DateTypeClass?
    'numeric': NumberTypeClass,
    'text': StringTypeClass,
    'timestamp with time zone': StringTypeClass,
    'integer': NumberTypeClass
}

def get_column_type(
    report: DBTSourceReport, dataset_name: str, column_type: str
) -> SchemaFieldDataType:
    """
    Maps known DBT types to datahub types
    """

    pattern = re.compile('[\w ]+') # drop all non alphanumerics
    match = pattern.match(column_type)
    column_type_stripped = match.group()

    TypeClass: Any = None
    for key in _field_type_mapping.keys():
        if key == column_type_stripped:
            TypeClass = _field_type_mapping[column_type_stripped]
            break

    if TypeClass is None:
        report.report_warning(
            dataset_name, f"unable to map type {column_type} to metadata schema"
        )
        TypeClass = NullTypeClass

    return SchemaFieldDataType(type=TypeClass())


def get_schema_metadata(
    report: DBTSourceReport, node: DBTNode, platform: str
) -> SchemaMetadata:
    canonical_schema: List[SchemaField] = []
    for column in node.columns:
        field = SchemaField(
            fieldPath=column.name,
            nativeDataType=column.data_type,
            type=get_column_type(report, node.dbt_name, column.data_type),
            description=column.comment,
        )
        canonical_schema.append(field)

    actor, sys_time = "urn:li:corpuser:dbt_executor", int(time.time()) * 1000
    schema_metadata = SchemaMetadata(
        schemaName=node.dbt_name,
        platform=f"urn:li:dataPlatform:{platform}",
        version=0,
        hash="",
        platformSchema=MySqlDDL(tableSchema=""),
        created=AuditStamp(time=sys_time, actor=actor),
        lastModified=AuditStamp(time=sys_time, actor=actor),
        fields=canonical_schema,
    )
    return schema_metadata

class DBTManifestSource(Source):
    """A Base class for all SQL Sources that use SQLAlchemy to extend"""
    @classmethod
    def create(cls, config_dict, ctx):
        config = DBTManifestConfig.parse_obj(config_dict)
        return cls(config, ctx, "dbt")

    def __init__(self, config: DBTManifestConfig, ctx: PipelineContext, platform: str):
        super().__init__(ctx)
        self.config = config
        self.platform = platform
        self.report = DBTSourceReport()

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        env: str = "PROD"
        sql_config = self.config
        platform = self.platform

        logger.info(self)

        nodes = loadManifestAndCatalog(self.config.manifest_path, self.config.catalog_path, platform, env)

        for node in nodes:
            logger.info(f"Converting node {node.dbt_name}")

            mce = MetadataChangeEvent()

            dataset_snapshot = DatasetSnapshot()
            dataset_snapshot.urn = node.datahub_urn
            logger.info(dataset_snapshot.urn)
            custom_properties = get_custom_properties(node)

            dbt_properties = DatasetPropertiesClass(
                description=node.dbt_name,
                customProperties=custom_properties
            )

            dataset_snapshot.aspects.append(dbt_properties)
            logger.info(f"Converting node {node}")

            upstreams = get_upstream_lineage(node.upstream_urns)
            if upstreams is not None:
                dataset_snapshot.aspects.append(upstreams)

            schema_metadata = get_schema_metadata(self.report, node, platform)
            dataset_snapshot.aspects.append(schema_metadata)

            mce.proposedSnapshot = dataset_snapshot
            wu = MetadataWorkUnit(id=dataset_snapshot.urn, mce=mce)
            yield  wu

    def get_report(self):
        return self.report

    def close(self):
        pass
