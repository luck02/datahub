import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.ingestion.api.common import PipelineContext, RecordEnvelope
from datahub.ingestion.transformer.add_dataset_ownership import (
    SimpleAddDatasetOwnership,
)


def test_simple_dataset_ownership_tranformation(mock_time):
    no_owner_aspect = models.MetadataChangeEventClass(
        proposedSnapshot=models.DatasetSnapshotClass(
            urn="urn:li:dataset:(urn:li:dataPlatform:bigquery,example1,PROD)",
            aspects=[
                models.StatusClass(removed=False),
            ],
        ),
    )
    with_owner_aspect = models.MetadataChangeEventClass(
        proposedSnapshot=models.DatasetSnapshotClass(
            urn="urn:li:dataset:(urn:li:dataPlatform:bigquery,example2,PROD)",
            aspects=[
                models.OwnershipClass(
                    owners=[
                        models.OwnerClass(
                            owner=builder.make_user_urn("fake_owner"),
                            type=models.OwnershipTypeClass.DATAOWNER,
                        ),
                    ],
                    lastModified=models.AuditStampClass(
                        time=builder.get_sys_time(), actor="urn:li:corpuser:datahub"
                    ),
                )
            ],
        ),
    )

    not_a_dataset = models.MetadataChangeEventClass(
        proposedSnapshot=models.DataJobSnapshotClass(
            urn="urn:li:dataJob:(urn:li:dataFlow:(airflow,dag_abc,PROD),task_456)",
            aspects=[
                models.DataJobInfoClass(
                    name="User Deletions",
                    description="Constructs the fct_users_deleted from logging_events",
                    type=models.AzkabanJobTypeClass.SQL,
                )
            ],
        )
    )

    inputs = [
        no_owner_aspect,
        with_owner_aspect,
        not_a_dataset,
    ]

    transformer = SimpleAddDatasetOwnership.create(
        {
            "owner_urns": [
                builder.make_user_urn("person1"),
                builder.make_user_urn("person2"),
            ]
        },
        PipelineContext(run_id="test"),
    )

    outputs = list(
        transformer.transform([RecordEnvelope(input, metadata={}) for input in inputs])
    )

    assert len(outputs) == len(inputs)

    # Check the first entry.
    first_ownership_aspect = builder.get_aspect_if_available(
        outputs[0].record, models.OwnershipClass
    )
    assert first_ownership_aspect
    assert len(first_ownership_aspect.owners) == 2

    # Check the second entry.
    second_ownership_aspect = builder.get_aspect_if_available(
        outputs[1].record, models.OwnershipClass
    )
    assert second_ownership_aspect
    assert len(second_ownership_aspect.owners) == 3

    # Verify that the third entry is unchanged.
    assert inputs[2] == outputs[2].record
