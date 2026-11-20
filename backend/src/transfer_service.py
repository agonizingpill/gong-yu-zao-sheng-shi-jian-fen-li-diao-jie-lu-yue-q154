import math
from fastapi import HTTPException
from sqlalchemy import select
from .models import Complaint,NoiseEvent,Separation,Attribution,Agreement,Recheck,Breach,TransferBatch,TransferItem
from .common import require,lock_case,get,record,now,dump
from .constants import BANDS

# 成分余弦相似度差值低于该值即视为两卷成分难以区分，不做建议，交主管裁定。
SIMILARITY_MARGIN=0.05
SOURCE_LABELS=('equipment','impact','speech')
BATCH_OPEN=('pending','decided')


def _weights_vector(item):
    return tuple(float(item['weights'].get(label,0.0)) for label in SOURCE_LABELS)


def _cosine(a,b):
    da=math.sqrt(sum(x*x for x in a));db_=math.sqrt(sum(y*y for y in b))
    return sum(x*y for x,y in zip(a,b))/(da*db_) if da and db_ else 0.0


def _job_item(job,event_id):
    if job is None or job.state!='done' or not job.result:return None
    for item in job.result.get('events',[]):
        if item.get('event_id')==event_id:return item
    return None


def _weights_for(db,complaint_id,event_id):
    for job in db.scalars(select(Separation).where(Separation.complaint_id==complaint_id,Separation.state=='done').order_by(Separation.id.desc())):
        item=_job_item(job,event_id)
        if item:return item
    return None


def _latest_attribution(db,complaint_id):
    return db.scalar(select(Attribution).where(Attribution.complaint_id==complaint_id).order_by(Attribution.id.desc()))


def protected_event_ids(db,complaint_id):
    """已用于支撑生效协议的采样：归因分离快照、复验事件与复发事件，必须留在原卷。"""
    ids=set()
    agreements=db.scalars(select(Agreement).where(Agreement.complaint_id==complaint_id,Agreement.activated_at.is_not(None))).all()
    for a in agreements:
        attr=db.get(Attribution,a.attribution_id)
        if attr:
            sep=db.get(Separation,attr.separation_id)
            if sep and sep.event_ids:ids.update(sep.event_ids)
        ids.update(db.scalars(select(Recheck.event_id).where(Recheck.agreement_id==a.id)))
        ids.update(db.scalars(select(Breach.event_id).where(Breach.agreement_id==a.id)))
    return ids


def _reference_options(db,complaint_id):
    """某卷可用于参照的已核实采样：须在最近一次已核实人工归因的完成快照内。"""
    attr=_latest_attribution(db,complaint_id)
    if attr is None or attr.source=='undetermined':return []
    sep=db.get(Separation,attr.separation_id)
    if sep is None or sep.state!='done' or not sep.result:return []
    out=[]
    for e in db.scalars(select(NoiseEvent).where(NoiseEvent.complaint_id==complaint_id).order_by(NoiseEvent.measured_at)):
        item=_job_item(sep,e.id)
        if item is None:continue
        out.append({'event_id':e.id,'measured_at':e.measured_at,'position':e.position,'total':e.total,
                    'dominant':item.get('dominant',''),'attribution_id':attr.id,'separation_id':sep.id})
    return out


def _reference_item(db,complaint_id,event_id,label):
    event=get(db,NoiseEvent,event_id)
    require(event.complaint_id==complaint_id,f'{label}参照采样须属于对应卷宗')
    attr=_latest_attribution(db,complaint_id)
    require(attr is not None and attr.source!='undetermined',f'{label}卷宗尚无已核实的人工归因，不能作为参照')
    sep=db.get(Separation,attr.separation_id)
    require(event_id in (sep.event_ids or []),f'{label}参照采样不在已核实的分离快照中，不能作为参照')
    item=_job_item(sep,event_id)
    require(item is not None,f'{label}参照采样缺少已完成成分，不能作为参照')
    return item


def _dominant_band(e):
    return BANDS[max(range(len(e.energies)),key=lambda i:e.energies[i])]


def _recommend(vec,ws,wt):
    if vec is None or ws is None or wt is None:return 'review'
    if abs(ws-wt)<SIMILARITY_MARGIN:return 'review'
    return 'target' if wt>ws else 'source'


def _basis(src_ref,tgt_ref,ws,wt,recommend):
    if ws is None or wt is None:
        return '缺少包含该采样的已完成成分分离，无法按成分比较，交主管裁定'
    diff=abs(ws-wt)
    head=f'与参照采样 #{src_ref}（原卷）/ #{tgt_ref}（目标卷）的成分余弦相似度：原卷 {ws:.3f}、目标卷 {wt:.3f}，差值 {diff:.3f}'
    if recommend=='source':return head+f' ≥ {SIMILARITY_MARGIN}，成分更接近原卷，建议留在原卷'
    if recommend=='target':return head+f' ≥ {SIMILARITY_MARGIN}，成分更接近目标卷，建议转目标卷'
    return head+f' < {SIMILARITY_MARGIN}，两卷成分难以区分，交主管裁定'


def _transfer_history(db,event_id):
    rows=db.scalars(select(TransferItem).where(TransferItem.event_id==event_id,TransferItem.transferred==True).order_by(TransferItem.id)).all()
    return [{'batch_id':i.batch_id,'original_complaint_id':i.original_complaint_id,
             'destination_complaint_id':i.destination_complaint_id} for i in rows]


def candidates(db,complaint_id):
    case=get(db,Complaint,complaint_id)
    protected=protected_event_ids(db,complaint_id)
    pending={}
    for item in db.scalars(select(TransferItem).join(TransferBatch,TransferItem.batch_id==TransferBatch.id)
                           .where(TransferBatch.state.in_(BATCH_OPEN),TransferItem.original_complaint_id==complaint_id)):
        pending[item.event_id]=item
    rows=db.scalars(select(NoiseEvent).where(NoiseEvent.complaint_id==complaint_id).order_by(NoiseEvent.measured_at,NoiseEvent.id)).all()
    events=[]
    for e in rows:
        item=_weights_for(db,complaint_id,e.id)
        pending_item=pending.get(e.id)
        events.append({'event_id':e.id,'measured_at':e.measured_at,'position':e.position,'duration_s':e.duration_s,
                       'total':e.total,'dominant_band':_dominant_band(e),
                       'weights':item['weights'] if item else None,'dominant':item.get('dominant','') if item else '',
                       'has_composition':item is not None,'protected':e.id in protected,
                       'pending_batch_id':pending_item.batch_id if pending_item else None,
                       'pending_batch_state':db.get(TransferBatch,pending_item.batch_id).state if pending_item else None,
                       'history':_transfer_history(db,e.id)})
    return {'case_id':complaint_id,'can_send':case.status!='closed',
            'events':events,
            'source_references':_reference_options(db,complaint_id),
            'references':[{'case_id':c.id,'code':c.code,'alias':c.alias,'status':c.status,'consent':c.consent,
                           'events':_reference_options(db,c.id)}
                          for c in db.scalars(select(Complaint).where(Complaint.id!=complaint_id).order_by(Complaint.id))]}


def create_batch(db,user,source_id,data):
    # 按编号升序锁两卷，与移交执行保持同一加锁顺序，避免交叉转卷死锁。
    for cid in sorted({source_id,data.target_id}):lock_case(db,cid)
    source=get(db,Complaint,source_id);target=get(db,Complaint,data.target_id)
    require(source.id!=target.id,'原卷与目标卷不能相同')
    require(source.status!='closed' and target.status!='closed','已结案卷宗不能转卷，请先由主管重开')
    require(target.consent,'目标卷监测授权未取得或已撤回，不能接收采样')
    protected=protected_event_ids(db,source_id)
    events=list(db.scalars(select(NoiseEvent).where(NoiseEvent.id.in_(data.event_ids))).all())
    found={e.id:e for e in events}
    missing=[i for i in data.event_ids if i not in found]
    require(not missing,f'采样不存在：{missing}')
    foreign=[i for i,e in found.items() if e.complaint_id!=source_id]
    require(not foreign,f'采样不属于原卷：{sorted(foreign)}')
    locked=sorted(i for i in found if i in protected)
    require(not locked,f'采样已用于支撑生效协议，必须留在原卷：{locked}')
    conflict=list(db.scalars(select(TransferItem).join(TransferBatch,TransferItem.batch_id==TransferBatch.id)
                            .where(TransferBatch.state.in_(BATCH_OPEN),TransferItem.event_id.in_(data.event_ids))).all())
    if conflict:raise HTTPException(409,f'采样已在未完成的转卷批次中：批次 QT{conflict[0].batch_id}')
    src_item=_reference_item(db,source_id,data.reference_source_event_id,'原卷')
    tgt_item=_reference_item(db,target.id,data.reference_target_event_id,'目标卷')
    src_vec=_weights_vector(src_item);tgt_vec=_weights_vector(tgt_item)
    batch=TransferBatch(source_id=source_id,target_id=target.id,reference_source_event_id=data.reference_source_event_id,
                        reference_target_event_id=data.reference_target_event_id,state='pending',note=data.note,created_by=user.username)
    db.add(batch);db.flush()
    undecided=0
    for e in sorted(found.values(),key=lambda x:(x.measured_at,x.id)):
        item=_weights_for(db,source_id,e.id)
        vec=_weights_vector(item) if item else None
        ws=_cosine(vec,src_vec) if vec else None
        wt=_cosine(vec,tgt_vec) if vec else None
        recommend=_recommend(vec,ws,wt)
        # 成分清晰的按建议直接定出归处；难分的留待主管逐条裁定。
        decision={'target':'target','source':'keep'}.get(recommend,'undecided')
        if decision=='undecided':undecided+=1
        db.add(TransferItem(batch_id=batch.id,event_id=e.id,original_complaint_id=source_id,event_position=e.position,
                            event_measured_at=e.measured_at,event_total=e.total,dominant_band=_dominant_band(e),
                            similarity_source=ws,similarity_target=wt,
                            margin=abs(ws-wt) if ws is not None and wt is not None else None,
                            recommendation=recommend,decision=decision,
                            basis=_basis(data.reference_source_event_id,data.reference_target_event_id,ws,wt,recommend)))
    # 全部可自动定处时无需裁定，直接进入待移交；否则等待主管处理难分条目。
    if not undecided:batch.state='decided';batch.decided_by=user.username;batch.decided_at=now(db);batch.decision_note='成分分布清晰，按建议自动定处'
    record(db,user,'transfer.create',batch.id,f'complaint={source_id}->{target.id};events={len(data.event_ids)};undecided={undecided}')
    db.commit();return batch.id


def _lock_batch(db,id):
    row=db.scalar(select(TransferBatch).where(TransferBatch.id==id).with_for_update().execution_options(populate_existing=True))
    if not row:raise HTTPException(404,'转卷批次不存在')
    return row


def batch_items(db,id):
    return list(db.scalars(select(TransferItem).where(TransferItem.batch_id==id).order_by(TransferItem.id)).all())


def batch_detail(db,id):
    batch=get(db,TransferBatch,id)
    return {**dump(batch),'items':[dump(i) for i in batch_items(db,id)],
            'source_code':db.get(Complaint,batch.source_id).code,'target_code':db.get(Complaint,batch.target_id).code}


def batches_for(db,complaint_id):
    rows=db.scalars(select(TransferBatch).where((TransferBatch.source_id==complaint_id)|(TransferBatch.target_id==complaint_id)).order_by(TransferBatch.id.desc())).all()
    out=[]
    for b in rows:
        out.append({**dump(b),'items':[dump(i) for i in batch_items(db,b.id)],
                    'source_code':db.get(Complaint,b.source_id).code,'target_code':db.get(Complaint,b.target_id).code})
    return out


def decide(db,user,id,data):
    batch=_lock_batch(db,id);require(batch.state=='pending','该批次不处于待裁定状态')
    items={i.id:i for i in batch_items(db,id)}
    foreign=[x.item_id for x in data.items if x.item_id not in items]
    require(not foreign,f'条目不属于该批次：{foreign}')
    undecided={i.id for i in items.values() if i.decision=='undecided'}
    untouched=undecided-{x.item_id for x in data.items}
    require(not untouched,f'须对难以区分的采样逐条裁定，未裁定条目：{sorted(untouched)}')
    for x in data.items:items[x.item_id].decision=x.decision
    batch.state='decided';batch.decision_note=data.note;batch.decided_by=user.username;batch.decided_at=now(db)
    record(db,user,'transfer.decide',id,data.note)
    db.commit();return id


def execute(db,user,id):
    batch=_lock_batch(db,id)
    if batch.state=='executed':
        record(db,user,'transfer.retry',id,'批次已执行，按原记录返回，不重复移交');db.commit();return id
    if batch.state=='returned':
        record(db,user,'transfer.retry',id,'批次已退回，未移交任何采样，须重新建立批次');db.commit();return id
    require(batch.state=='decided','主管尚未完成裁定，不能移交')
    for cid in sorted({batch.source_id,batch.target_id}):lock_case(db,cid)
    source=db.get(Complaint,batch.source_id);target=db.get(Complaint,batch.target_id)
    items=batch_items(db,id)
    approved=[i for i in items if i.decision=='target']
    reasons=[]
    if source.status=='closed' or target.status=='closed':reasons.append('原卷或目标卷已结案')
    if not target.consent:reasons.append('目标卷监测授权已撤回')
    # 协议与转卷可能同时提交：执行时按锁内最新证据重新核对生效协议依赖的采样。
    protected=protected_event_ids(db,batch.source_id)
    seen=set()
    for item in approved:
        event=db.get(NoiseEvent,item.event_id)
        if event is None or event.complaint_id!=batch.source_id:
            reasons.append(f'采样 #{item.event_id} 已不在原卷')
            continue
        if item.event_id in protected:reasons.append(f'采样 #{item.event_id} 已用于支撑生效协议')
        key=(event.measured_at,event.position)
        if key in seen:reasons.append(f'本批内采样 #{item.event_id} 与另一采样同测点同时刻')
        seen.add(key)
        collision=db.scalar(select(NoiseEvent).where(NoiseEvent.complaint_id==batch.target_id,
                                                     NoiseEvent.measured_at==event.measured_at,
                                                     NoiseEvent.position==event.position))
        if collision:reasons.append(f'采样 #{item.event_id} 与目标卷记录 #{collision.id}（{event.position} / {event.measured_at}）同测点同时刻')
    if reasons:
        batch.state='returned';batch.outcome='整批退回，未移交任何采样：'+'；'.join(dict.fromkeys(reasons))
        batch.executed_by=user.username;batch.executed_at=now(db)
        record(db,user,'transfer.return',id,batch.outcome);db.commit();return id
    if not approved:
        for item in items:item.destination_complaint_id=batch.source_id
        batch.state='executed';batch.executed_by=user.username;batch.executed_at=now(db)
        batch.outcome='主管裁定全部留在原卷，无需移交'
        record(db,user,'transfer.execute',id,batch.outcome);db.commit();return id
    for item in approved:
        event=db.get(NoiseEvent,item.event_id)
        event.complaint_id=batch.target_id
        item.transferred=True;item.destination_complaint_id=batch.target_id
        record(db,user,'event.transfer',event.id,f'QT{batch.id}: {batch.source_id}->{batch.target_id}')
    for item in items:
        if item.decision!='target':item.destination_complaint_id=batch.source_id
    batch.state='executed';batch.executed_by=user.username;batch.executed_at=now(db)
    batch.outcome=f'已整批移交 {len(approved)} 条采样至目标卷'
    record(db,user,'transfer.execute',id,batch.outcome);db.commit();return id
