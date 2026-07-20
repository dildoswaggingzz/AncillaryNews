import httpx
import pytest
import respx
from tenacity import wait_none

from shared.base_ingestor import BaseIngestor
from shared.dataset_validation import missing_fields
from shared.datasets import DATASETS, DatasetConfig, SeriesConfig

BASE_URL = "https://api.example.test"


@pytest.fixture
def ingestor():
    # Disable exponential backoff so failure-path tests run instantly (same
    # pattern as tests/test_base_ingestor.py).
    BaseIngestor.fetch_data.retry.wait = wait_none()
    ing = BaseIngestor(BASE_URL)
    yield ing


def _meta_response(columns: list[str]) -> dict:
    """Shape of a real `meta/dataset/{id}` response, trimmed to just what
    missing_fields reads (every real column also carries dataType/unit/etc,
    which this module never looks at)."""
    return {"columns": [{"dbColumn": c} for c in columns]}


TEST_DATASET = DatasetConfig(
    name="test_dataset",
    dataset_id="TestDataset",
    market="test_market",
    time_field="TimeUTC",
    zone_field="PriceArea",
    series=[
        SeriesConfig(product="up", value_field="UpPriceDKK"),
        SeriesConfig(product="down", value_field="DownPriceDKK"),
    ],
)


# --- meta/dataset as primary source ------------------------------------------


@respx.mock
async def test_missing_fields_empty_when_every_configured_field_present(ingestor):
    respx.get(f"{BASE_URL}/meta/dataset/TestDataset").mock(
        return_value=httpx.Response(
            200, json=_meta_response(["TimeUTC", "PriceArea", "UpPriceDKK", "DownPriceDKK"])
        )
    )

    result = await missing_fields(ingestor, TEST_DATASET)

    assert result == {}
    await ingestor.close()


@respx.mock
async def test_missing_fields_catches_typo_d_value_field(ingestor):
    """The primary regression case: a value_field that doesn't exist in the
    published schema at all -- a typo, not a null column -- must be
    reported, categorized under 'value_field'."""
    respx.get(f"{BASE_URL}/meta/dataset/TestDataset").mock(
        return_value=httpx.Response(
            200,
            json=_meta_response(["TimeUTC", "PriceArea", "UpPriceDKK"]),  # DownPriceDKK typo'd
        )
    )

    result = await missing_fields(ingestor, TEST_DATASET)

    assert result == {"value_field": ["DownPriceDKK"]}
    await ingestor.close()


@respx.mock
async def test_missing_fields_catches_missing_time_and_zone_field(ingestor):
    respx.get(f"{BASE_URL}/meta/dataset/TestDataset").mock(
        return_value=httpx.Response(200, json=_meta_response(["UpPriceDKK", "DownPriceDKK"]))
    )

    result = await missing_fields(ingestor, TEST_DATASET)

    assert result == {"time_field": ["TimeUTC"], "zone_field": ["PriceArea"]}
    await ingestor.close()


@respx.mock
async def test_missing_fields_reports_missing_filter_field_and_extra_filters(ingestor):
    dataset = DatasetConfig(
        name="filtered_test",
        dataset_id="FilteredTest",
        market="test_market",
        time_field="HourUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(
                product="price",
                value_field="PriceTotalEUR",
                filter_field="ProductNmae",  # typo'd
                filter_value="FCR-N",
                extra_filters={"AuctionTyp": "Total"},  # typo'd
            ),
        ],
    )
    respx.get(f"{BASE_URL}/meta/dataset/FilteredTest").mock(
        return_value=httpx.Response(
            200,
            json=_meta_response(
                ["HourUTC", "PriceArea", "PriceTotalEUR", "ProductName", "AuctionType"]
            ),
        )
    )

    result = await missing_fields(ingestor, dataset)

    assert result == {"filter_field": ["AuctionTyp", "ProductNmae"]}
    await ingestor.close()


@respx.mock
async def test_missing_fields_does_not_flag_a_field_that_is_merely_null_in_practice(ingestor):
    """
    The exact scenario the module docstring calls out: MfrrCapacityMarketExtra's
    price columns are entirely null in live data today, but they ARE part
    of the published schema -- meta/dataset (the primary source) reports
    them as present regardless of what any sample's values look like, so
    they must never be flagged as missing.
    """
    dataset = DatasetConfig(
        name="mfrr_extra_test",
        dataset_id="MfrrCapacityMarketExtra",
        market="mFRR_capacity_extra",
        time_field="TimeUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="up", value_field="UpPriceDKK"),
            SeriesConfig(product="down", value_field="DownPriceDKK"),
        ],
    )
    respx.get(f"{BASE_URL}/meta/dataset/MfrrCapacityMarketExtra").mock(
        return_value=httpx.Response(
            200,
            json=_meta_response(
                ["TimeUTC", "PriceArea", "UpPriceDKK", "DownPriceDKK", "UpDemandMW"]
            ),
        )
    )

    result = await missing_fields(ingestor, dataset)

    assert result == {}
    await ingestor.close()


# --- fallback to a live record sample -----------------------------------------


@respx.mock
async def test_missing_fields_falls_back_to_sample_when_meta_call_fails(ingestor):
    respx.get(f"{BASE_URL}/meta/dataset/TestDataset").mock(return_value=httpx.Response(500))
    respx.get(f"{BASE_URL}/dataset/TestDataset").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [
                    {"TimeUTC": "t", "PriceArea": "DK1", "UpPriceDKK": 1.0, "DownPriceDKK": 2.0}
                ]
            },
        )
    )

    result = await missing_fields(ingestor, TEST_DATASET)

    assert result == {}
    await ingestor.close()


@respx.mock
async def test_missing_fields_falls_back_when_meta_has_no_columns_list(ingestor):
    respx.get(f"{BASE_URL}/meta/dataset/TestDataset").mock(
        return_value=httpx.Response(200, json={"datasetId": 1})  # no "columns" key at all
    )
    respx.get(f"{BASE_URL}/dataset/TestDataset").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [
                    {"TimeUTC": "t", "PriceArea": "DK1", "UpPriceDKK": 1.0, "DownPriceDKK": 2.0}
                ]
            },
        )
    )

    result = await missing_fields(ingestor, TEST_DATASET)

    assert result == {}
    await ingestor.close()


@respx.mock
async def test_missing_fields_fallback_still_catches_a_genuine_typo(ingestor):
    respx.get(f"{BASE_URL}/meta/dataset/TestDataset").mock(return_value=httpx.Response(500))
    respx.get(f"{BASE_URL}/dataset/TestDataset").mock(
        return_value=httpx.Response(
            200, json={"records": [{"TimeUTC": "t", "PriceArea": "DK1", "UpPriceDKK": 1.0}]}
        )
    )

    result = await missing_fields(ingestor, TEST_DATASET)

    assert result == {"value_field": ["DownPriceDKK"]}
    await ingestor.close()


@respx.mock
async def test_missing_fields_fallback_unions_keys_across_every_sample_record(ingestor):
    """A field null on record 1 but present on record 2 must not be
    reported -- the fallback unions keys across the whole sample, since
    (unlike meta/dataset) it has no other way to know a field is real."""
    respx.get(f"{BASE_URL}/meta/dataset/TestDataset").mock(return_value=httpx.Response(500))
    respx.get(f"{BASE_URL}/dataset/TestDataset").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [
                    {"TimeUTC": "t1", "PriceArea": "DK1", "UpPriceDKK": 1.0},
                    {"TimeUTC": "t2", "PriceArea": "DK1", "DownPriceDKK": 2.0},
                ]
            },
        )
    )

    result = await missing_fields(ingestor, TEST_DATASET)

    assert result == {}
    await ingestor.close()


# --- live whole-registry sweep ------------------------------------------------


@pytest.mark.live
async def test_every_registered_dataset_matches_its_live_published_schema():
    """
    Live sweep across the entire shared/datasets.py registry against
    api.energidataservice.dk -- the actual pre-merge gate this module and
    scripts/validate_datasets.py exist for. Excluded from the default
    `poetry run pytest` run (see pyproject.toml's `addopts`); run explicitly
    with `poetry run pytest -m live`.
    """
    live_ingestor = BaseIngestor("https://api.energidataservice.dk")
    try:
        failures: dict[str, dict[str, list[str]]] = {}
        for dataset in DATASETS:
            missing = await missing_fields(live_ingestor, dataset)
            if missing:
                failures[dataset.name] = missing
        assert failures == {}, f"registry/live-schema mismatch(es): {failures}"
    finally:
        await live_ingestor.close()
