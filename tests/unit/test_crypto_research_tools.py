from types import SimpleNamespace
import pytest
from finplan_tools.core.registry import CATALOG
from finplan_tools.tools.crypto_research import call


def test_crypto_tools_are_read_only_and_beta_scoped():
    for name in ('get_crypto_experiment','recommend_crypto_portfolio','list_crypto_history','discover_research_data'):
        assert CATALOG[name].state_changing is False
        assert CATALOG[name].min_producer_contract=='1.7.0'
    with pytest.raises(Exception):call(SimpleNamespace(environment='prod'),{})


def test_adapter_uses_owned_classical_backend():
    calls=[]
    def backend(op,body,meta):calls.append((op,body));return {'synthetic':True}
    ctx=SimpleNamespace(environment='beta',jobs=SimpleNamespace(classical=backend),spec=SimpleNamespace(name='get_crypto_experiment'),meta=None)
    assert call(ctx,{})['synthetic']
    assert calls==[('get_crypto_experiment',{})]
