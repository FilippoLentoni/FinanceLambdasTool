"""Registered numeric provenance admits exact public citations, never private locations."""
import json
from copy import deepcopy

import pytest

from finplan_tools.core.artifacts import response_leaks, storage_input_problems
from tests.unit.test_classical_tools import Client, evidence, wire

V1 = 'gdelt-gkg-v1-organization-tone-v1'
V2 = 'gdelt-gkg-v2-premarket-organization-tone-v1'
ATTRIBUTION = 'The GDELT Project, https://www.gdeltproject.org/'
PRIVATE_LOCATION = 's3:' + '//private-example/evidence.json'


def lineage(version):
    result = {'attribution': ATTRIBUTION, 'rights': {'evidence_url': 'https://gdeltproject.org/about.html', 'attribution': ATTRIBUTION}}
    if version == V1:
        result.update(url='https://data.gdeltproject.org/gkg/20260102.gkg.csv.zip', rights_evidence_url='https://gdeltproject.org/about.html')
    else:
        result.update(sources=[{'url': 'https://data.gdeltproject.org/gdeltv2/20260102124500.gkg.csv.zip'}],
                      attempts=[{'url': 'https://data.gdeltproject.org/gdeltv2/20260102124500.gkg.csv.zip'}])
    return result


def news(version):
    return {'feature_protocol': version, 'consumed': True, 'provenance': {'lineage': lineage(version)}}


@pytest.mark.parametrize('version', [V1, V2])
def test_actual_shaped_numeric_citations_survive_the_full_tool_pipeline_unchanged(offline, invoke, version):
    document = evidence()
    document['recommendation'].setdefault('diagnostics', {})['news_evidence'] = news(version)
    original = deepcopy(document)
    wire(offline, Client(document))
    response = invoke(offline, 'generate_portfolio_recommendation_min_variance', {'instruments': ['SYNTH']})
    assert response == original
    assert not response_leaks(response)
    assert document == original


@pytest.mark.parametrize('version', [V1, V2])
def test_frozen_policy_source_documents_and_canonical_strings_receive_the_same_complete_scan(version):
    document = {'feature_version': version, 'lineage': lineage(version), 'source_day': '2026-01-02'}
    wrapper = {'document': document, 'canonical_document_json': json.dumps(document, sort_keys=True)}
    response = {'input_differences': {'previous': {'news_dataset': {'format': 'finplan.gdelt-corpus/1', 'feature_version': version, 'documents': [wrapper]}}}}
    assert not response_leaks(response)
    document['unrelated_private_metadata'] = PRIVATE_LOCATION
    wrapper['canonical_document_json'] = json.dumps(document, sort_keys=True)
    leaks = response_leaks(response)
    assert any(path.endswith('/unrelated_private_metadata') for path in leaks)
    assert any(path.endswith('/canonical_document_json') for path in leaks)


@pytest.mark.parametrize('location', [
    'https://example.execute-api.us-east-2.amazonaws.com/beta', PRIVATE_LOCATION,
    'https://127.0.0.1/private', 'https://user:password@data.gdeltproject.org/gkg/20260102.gkg.csv.zip',
    'https://data.gdeltproject.org.evil.invalid/gkg/20260102.gkg.csv.zip',
    'https://data.gdeltproject.org/gkg/20260102.gkg.csv.zip?X-Amz-Signature=private',
    'https://data.gdeltproject.org:8443/gkg/20260102.gkg.csv.zip',
])
def test_known_lineage_does_not_exempt_arbitrary_or_private_urls(offline, invoke, location):
    document = evidence()
    proof = news(V1)
    proof['provenance']['lineage']['url'] = location
    document['recommendation'].setdefault('diagnostics', {})['news_evidence'] = proof
    wire(offline, Client(document))
    response = invoke(offline, 'generate_portfolio_recommendation_min_variance', {'instruments': ['SYNTH']})
    assert response['code'] == 'INTERNAL'
    assert location not in json.dumps(response)


def test_exact_attribution_does_not_exempt_private_sidecars_or_appended_locations():
    proof = news(V1)
    proof['provenance']['lineage']['private_sidecar'] = PRIVATE_LOCATION
    assert response_leaks(proof) == ['/provenance/lineage/private_sidecar']
    proof['provenance']['lineage']['attribution'] += ' ' + PRIVATE_LOCATION
    assert '/provenance/lineage/attribution' in response_leaks(proof)
    assert response_leaks({'attribution': ATTRIBUTION}) == ['/attribution']
    assert storage_input_problems({'url': lineage(V1)['url']}) == ['/url']


def test_canonical_document_string_must_match_the_structured_original():
    document = {'feature_version': V1, 'lineage': lineage(V1)}
    mismatched = deepcopy(document)
    mismatched['private_field'] = 'https://example.execute-api.us-east-2.amazonaws.com/beta'
    assert '/canonical_document_json' in response_leaks({'document': document, 'canonical_document_json': json.dumps(mismatched)})
