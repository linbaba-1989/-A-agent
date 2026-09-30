from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from src.market_clock import BEIJING_TZ
from src.market_data.hithink_reference import ReferenceCache
from src.market_data.hithink_client import ApiResult
from src.market_data.sector_service import SectorService


def setup(tmp_path):
    cache=ReferenceCache(tmp_path,lambda:datetime(2026,9,29,22,tzinfo=BEIJING_TZ))
    rows={'industry':[dict(index_thscode='881101.TI',index_name='one',tag='industry')],
          'cn_concept':[dict(index_thscode='885540.TI',index_name='two',tag='cn_concept')]}
    for tag,items in rows.items():cache.write('catalog_'+tag,dict(item=items))
    cache.write('members_881101.TI',dict(item=[dict(symbol='600498.SH')]))
    def no_network(*args):raise AssertionError('unsupported group must not request members')
    return SectorService(SimpleNamespace(cache=cache,
        catalog=lambda tag:ApiResult('CACHED',data=dict(item=rows[tag])),members=no_network))


def test_supported_scope_persists_and_skips_network(tmp_path):
    service=setup(tmp_path)
    service.record_unsupported_market('885540.TI',[{'thscode':'600498.SH'},{'thscode':'834683.NQ'}])
    other=SectorService(service.provider)
    assert other.sync()=='COMPLETE_SUPPORTED_SCOPE'
    data=other.memberships('600498.SH')
    assert (data['catalog_total'],data['supported_total'],data['unsupported_total'])==(2,1,1)
    assert data['cached_supported']==1 and data['supported_coverage']==1
    assert data['quality_status']=='COMPLETE_SUPPORTED_SCOPE'
    assert other.members('885540.TI').status=='UNSUPPORTED_MARKET'
    assert other.provider.cache.read('members_885540.TI') is None
    other.provider.cache.clock=lambda:datetime(2026,10,2,22,tzinfo=BEIJING_TZ)
    assert other.members('885540.TI').status=='UNSUPPORTED_MARKET'
    stale=other.memberships('600498.SH')
    assert stale['quality_status']=='COMPLETE_SUPPORTED_SCOPE'
    assert stale['stale_members']==1


@pytest.mark.parametrize('rows',[[{'thscode':'bad'}],[{'thscode':'600498.SH'}]])
def test_parse_failure_or_supported_members_not_mislabeled(tmp_path,rows):
    service=setup(tmp_path)
    with pytest.raises(ValueError):service.record_unsupported_market('885540.TI',rows)
    assert not service.unsupported()


def test_scope_banner_discloses_excluded_group(tmp_path,monkeypatch):
    from ui.components import hybrid_status
    service=setup(tmp_path)
    service.record_unsupported_market('885540.TI',[{'thscode':'834683.NQ'}])
    captions=[]
    monkeypatch.setattr(hybrid_status.st,'caption',captions.append)
    provider=SimpleNamespace(provider_status=lambda:dict(realtime='tencent:AVAILABLE',history='hithink',
        universe='hithink',index='hithink',official_auth='CONFIGURED'),get_stock_sectors=service.memberships)
    hybrid_status.render_hybrid_status(provider)
    assert '1/2' in captions[1] and 'unsupported=1' in captions[1]
    assert 'COMPLETE_SUPPORTED_SCOPE' in captions[1]


def test_expired_disk_membership_is_reported_as_stale_cached_scope(tmp_path):
    clock=[datetime(2026,9,29,16,tzinfo=BEIJING_TZ)]
    cache=ReferenceCache(tmp_path,lambda:clock[0])
    catalogs={'industry':[dict(index_thscode='881101.TI',index_name='one',tag='industry')],
              'cn_concept':[dict(index_thscode='885540.TI',index_name='two',tag='cn_concept')]}
    for tag,items in catalogs.items():cache.write('catalog_'+tag,dict(item=items))
    cache.write('members_881101.TI',dict(item=[dict(symbol='600498.SH')]))
    provider=SimpleNamespace(cache=cache,catalog=lambda tag:ApiResult('CACHED',data=dict(item=catalogs[tag])))
    service=SectorService(provider)
    service.record_unsupported_market('885540.TI',[{'thscode':'834683.NQ'}])
    clock[0]=datetime(2026,9,30,18,tzinfo=BEIJING_TZ)
    data=service.memberships('600498.SH')
    assert data['covered']==1 and data['supported_total']==1
    assert data['stale_members']==1 and data['quality_status']=='COMPLETE_SUPPORTED_SCOPE'
    assert data['unsupported_total']==1


def test_sync_skips_expired_disk_membership_without_network(tmp_path):
    clock=[datetime(2026,9,29,16,tzinfo=BEIJING_TZ)]
    cache=ReferenceCache(tmp_path,lambda:clock[0])
    rows={'industry':[dict(index_thscode='881101.TI',index_name='one')],
          'cn_concept':[dict(index_thscode='885540.TI',index_name='two')]}
    for tag,items in rows.items():cache.write('catalog_'+tag,dict(item=items))
    cache.write('members_881101.TI',dict(item=[]))
    service=SectorService(SimpleNamespace(cache=cache,
        catalog=lambda tag:ApiResult('CACHED',data=dict(item=rows[tag])),
        members=lambda *a: (_ for _ in ()).throw(AssertionError('must not resync stale cache'))))
    service.record_unsupported_market('885540.TI',[{'thscode':'834683.NQ'}])
    clock[0]=datetime(2026,9,30,18,tzinfo=BEIJING_TZ)
    assert service.sync(max_requests=1)=='COMPLETE_SUPPORTED_SCOPE'
