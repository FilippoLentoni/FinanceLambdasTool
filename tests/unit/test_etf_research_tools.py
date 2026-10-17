from types import SimpleNamespace
import pytest
from finplan_tools.core.registry import CATALOG
from finplan_tools.tools.etf_research import get_etf_research_inputs


def test_etf_reader_backend_and_environment_gate():
    entry=CATALOG['get_etf_research_inputs']
    assert not entry.state_changing and entry.role_class=='reader' and entry.min_producer_contract=='1.8.0'
    calls=[]
    def backend(*args):calls.append(args);return {'contract_version':'1.8.0','features':{}}
    ctx=SimpleNamespace(environment='beta',spec=SimpleNamespace(name='get_etf_research_inputs'),jobs=SimpleNamespace(classical=backend),meta=None)
    result=get_etf_research_inputs(ctx,{'symbols':['SPY']})
    assert calls==[('get_etf_research_inputs',{'symbols':['SPY']},None)]
    ctx.environment='prod'
    with pytest.raises(Exception):get_etf_research_inputs(ctx,{'symbols':['SPY']})


def news_document(url='https://finance.yahoo.com/news/example-123456789012.html',title='ETF and Alphabet market research'):
    return {'decision_at':'2026-10-17T12:00:00Z','symbols':['VOO','GOOGL'],
            'feature_protocol':'etf-search-metadata/1','features':{},'provenance':[],
            'models':{'llm':{'context':[{'url':url,'title':title,'published_at':'2026-10-17T10:00:00Z','symbols':['VOO','GOOGL']}]}},
            'limitations':['synthetic_fixture'],'contract_version':'1.9.0'}


def test_valid_public_news_citation_survives_actual_response_guard(offline,invoke):
    from tests.unit.test_classical_tools import Client,wire
    document=news_document()
    wire(offline,Client(document))
    assert invoke(offline,'get_etf_research_inputs',{'symbols':['VOO','GOOGL']})==document


@pytest.mark.parametrize('url',[
    'https://private-bucket.s3.us-east-2.amazonaws.com/secret',
    'https://127.0.0.1/news','https://10.0.0.1/news','https://news.internal/article',
    'file:///private/news','https://user:password@finance.yahoo.com/news',
])
def test_nonpublic_citation_urls_are_rejected_before_leaf_exemption(offline,invoke,url):
    from tests.unit.test_classical_tools import Client,wire
    wire(offline,Client(news_document(url=url)))
    result=invoke(offline,'get_etf_research_inputs',{'symbols':['VOO','GOOGL']})
    assert result['code']=='INTERNAL'
    assert 'invalid public evidence citation' in result['message']


def test_public_url_does_not_exempt_title_or_other_storage_metadata(offline,invoke):
    from tests.unit.test_classical_tools import Client,wire
    arn='arn:aws:s3:::private-news-bucket'
    document=news_document(title='Untrusted headline '+arn)
    wire(offline,Client(document))
    result=invoke(offline,'get_etf_research_inputs',{'symbols':['VOO','GOOGL']})
    assert result['code']=='INTERNAL' and 'storage location' in result['message']
    assert arn not in result['message']
