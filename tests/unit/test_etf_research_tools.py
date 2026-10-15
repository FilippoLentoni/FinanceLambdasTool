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
