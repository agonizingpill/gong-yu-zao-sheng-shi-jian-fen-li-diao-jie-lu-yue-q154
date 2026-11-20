from typing import Literal
from pydantic import BaseModel,Field,field_validator
class TransferBatchInput(BaseModel):
    target_id:int=Field(gt=0)
    reference_source_event_id:int=Field(gt=0)
    reference_target_event_id:int=Field(gt=0)
    event_ids:list[int]=Field(min_length=1,max_length=200)
    note:str=Field(min_length=6,max_length=400)
    @field_validator('event_ids')
    @classmethod
    def unique_ids(cls,v):
        if any(x<=0 for x in v):raise ValueError('采样编号须为正整数')
        if len(set(v))!=len(v):raise ValueError('同一采样不能在一批中重复列出')
        return v
class ItemDecision(BaseModel):
    item_id:int=Field(gt=0)
    decision:Literal['target','keep']
class TransferDecisionInput(BaseModel):
    items:list[ItemDecision]=Field(min_length=1,max_length=200)
    note:str=Field(min_length=6,max_length=400)
    @field_validator('items')
    @classmethod
    def unique_items(cls,v):
        if len({x.item_id for x in v})!=len(v):raise ValueError('同一批次条目不能重复裁定')
        return v
    @field_validator('note')
    @classmethod
    def stripped(cls,v):
        if not v.strip():raise ValueError('裁定说明不可全为空格')
        return v.strip()
