import socket
import pytest
from evals.a_share.p193f_fact_relations import semantic_relation_violations, derive_fact_relations, assertion_context
from evals.a_share.p193_role_ab import load_fixture

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*a, **kw): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket,'connect',fail)

@pytest.mark.parametrize('metric,text,expected',[
 ('numeric_relation_violation','当前已突破52.3',True),
 ('numeric_relation_violation','52.0 >= 52.3',True),
 ('numeric_relation_violation','尚未突破52.3',False),
 ('numeric_relation_violation','若突破52.3',False),
 ('numeric_relation_violation','后续突破52.3',False),
 ('numeric_relation_violation','突破52.3后风险下降',False),
 ('numeric_relation_violation','突破52.3将构成确认',False),
 ('temporal_semantics_violation','今日收于52.0',True),
 ('temporal_semantics_violation','收盘接近当日低位',True),
 ('temporal_semantics_violation','截至收盘',True),
 ('temporal_semantics_violation','尚未收盘',False),
 ('temporal_semantics_violation','未获得收盘确认',False),
 ('temporal_semantics_violation','若收盘跌破MA20',False),
 ('temporal_semantics_violation','收盘后再确认',False),
 ('trade_instruction_violation','建议立即买入',True),
 ('trade_instruction_violation','建议买入30%仓位',True),
 ('trade_instruction_violation','明日开盘买入',True),
 ('trade_instruction_violation','T+1买入后不能当日卖出',False),
 ('trade_instruction_violation','买入行为存在流动性风险',False),
 ('trade_instruction_violation','若投资者买入，需承担流动性风险',False),
 ('provenance_violation','QMT数据显示上涨',True),
 ('provenance_violation','缺少QMT数据',False),
 ('provenance_violation','QMT数据不可用',False),
 ('semantic_transformation_violation','20日区间位置90%等于20日90分位',True),
 ('semantic_transformation_violation','接近20日区间上沿',False),
 ('unsupported_trajectory_claim','MA60已经拐头',True),
 ('unsupported_trajectory_claim','长期均线尚未拐头',True),
 ('unsupported_trajectory_claim','MA60正在上升',True),
 ('unsupported_trajectory_claim','仅有price<MA60',False),
 ('unsupported_trajectory_claim','缺少MA60历史，无法判断是否拐头',False),
])
def test_context_boundaries(metric,text,expected):
    assert semantic_relation_violations({'summary':text},load_fixture('GC05')['facts'])[metric] is expected

@pytest.mark.parametrize('text,bad',[
 ('公司公告显示获得10亿元订单',True),
 ('公司公告数据不可用，因此无法确认订单',False),
 ('缺公告所以无法确认订单',False),
])
def test_topic_evidence(text,bad):
    result=semantic_relation_violations({'summary':text,'evidence':[{'claim':'现价52','status':'confirmed'}]},load_fixture('GC05')['facts'])
    assert bool(result['unavailable_to_confirmed']) is bad

def test_evidence_status_scoped_to_claim():
    facts=load_fixture('GC05')['facts']
    assert semantic_relation_violations({'evidence':[{'claim':'公司公告存在订单','status':'confirmed'}]},facts)['unavailable_to_confirmed']
    assert not semantic_relation_violations({'missing_evidence':['公司公告'],'evidence':[{'claim':'当前价格52.0','status':'confirmed'}]},facts)['unavailable_to_confirmed']

def test_negative_and_current_claims_in_one_sentence():
    facts=load_fixture('GC05')['facts']
    assert semantic_relation_violations({'summary':'尚未收盘，但当前已突破52.3'},facts)['numeric_relation_violation']

def test_deterministic_boundaries_and_explicit_support():
    facts=load_fixture('GC05')['facts'];relations=derive_fact_relations(facts)
    assert relations['range_position_20d_percent']==90
    assert relations['range_position_is_percentile'] is False
    assert relations['close_price_available'] is False
    assert relations['ma60_slope_available'] is False
    assert not semantic_relation_violations({'summary':'MA60正在上升，20日90分位'},{**facts,'ma60_slope':1,'percentile_20d':90})['unsupported_trajectory_claim']
    assert not semantic_relation_violations({'summary':'20日90分位'},{**facts,'percentile_20d':90})['semantic_transformation_violation']

@pytest.mark.parametrize('text,expected',[('已突破52.3','CURRENT_ASSERTION'),('若突破52.3','CONDITIONAL'),('后续突破52.3','FUTURE_SCENARIO'),('未站上52.3','NEGATED_ASSERTION')])
def test_context_labels(text,expected):
    assert assertion_context(text)==expected

def test_g_saved_response_equivalent_structures():
    tech = semantic_relation_violations({
        'bearish_signals':['日内位置18.75%，收盘接近当日低位'],
        'moving_average_structure':'长期均线尚未拐头',
        'invalidation_conditions':[{'condition':'价格有效突破近期高点104.2并守住（需放量确认）','evidence_source':'价格、均线、成交量'}],
        'evidence':[{'source':'synthetic_intraday_snapshot','claim':'价格101.0','status':'confirmed'}]
    },load_fixture('GC01')['facts'])
    assert tech['temporal_semantics_violation'] and tech['unsupported_trajectory_claim']
    assert not tech['numeric_relation_violation'] and not tech['provenance_violation']
    risk=semantic_relation_violations({
        'bull_case_challenges':['price已处20日90分位'],
        'invalid_assumptions':['不得用收盘或日终确认描述14:30盘中快照'],
        'invalidation_conditions':[{'condition':'有效突破52.3失败后回落至MA10=50.8下方','timeframe':'收盘确认，非14:30盘中'}],
        'missing_evidence':['无完整日K与收盘确认'],
        'positioning_risks':['T+1规则下当日买入无法当日退出']
    },load_fixture('GC05')['facts'])
    assert risk['semantic_transformation_violation']
    assert not any(risk[k] for k in ('numeric_relation_violation','temporal_semantics_violation','provenance_violation','trade_instruction_violation'))

def test_eval_both_roles_get_boundaries_qwen_unchanged(tmp_path):
    from evals.a_share.p193_role_ab import RoleIsolatedRunner, FACT_SEMANTIC_DISCIPLINE
    from src.usage_tracker import UsageTracker
    runner=RoleIsolatedRunner(tracker=UsageTracker(tmp_path/'unused.jsonl'))
    for role,case in [('technical_analyst','GC01'),('risk_officer','GC05')]:
        request=runner.prepare_request(role,load_fixture(case),False,'offline')
        assert FACT_SEMANTIC_DISCIPLINE in request['messages'][0]['content']
        assert request['derived_fact_relations']['close_price_available'] is False
        assert request['selected_case_ids']==[]
    qwen=runner.prepare_request('fundamental_event_analyst',load_fixture('GC04'),False,'offline')
    assert qwen['prompt_hash']=='18b41634f6154467c78ec5e06823be0c876b00b364781314c789e8bae087af0f'
    assert FACT_SEMANTIC_DISCIPLINE not in qwen['messages'][0]['content']
    plan=runner.build_baseline_recheck_plan()
    assert plan['planned_calls']==plan['max_provider_attempts']==2
    assert plan['few_shot_on_calls']==0

@pytest.mark.parametrize('metric', ['semantic_transformation_violation','unsupported_trajectory_claim','trade_instruction_violation'])
def test_new_hard_fails_block_gate(metric):
    from test_p193d_baseline import _passing_baseline_rows
    from evals.a_share.p193_role_ab import baseline_reliability_gate
    rows=_passing_baseline_rows()
    rows[0]['metrics'][metric]=True
    review={r['role']:True for r in rows}
    assert baseline_reliability_gate(rows,review)['status']=='FAIL'

def test_old_keyword_metrics_recomputed_without_erasing_saved_data():
    from copy import deepcopy
    from test_p193d_baseline import _passing_baseline_rows
    from evals.a_share.p193_role_ab import baseline_reliability_gate
    rows=_passing_baseline_rows()
    rows[0]['status']='SEMANTIC_FAIL'
    rows[0]['metrics']['numeric_relation_violation']=True
    rows[0]['parsed_response']={'summary':'若后续突破104.2，则需要确认','missing_evidence':['基本面数据']}
    before=deepcopy(rows)
    assert baseline_reliability_gate(rows,{r['role']:True for r in rows})['status']=='PASS'
    assert rows==before
