from fastapi import APIRouter,Depends
from .database import session
from .auth import current,operator,supervisor
from .transfer_schemas import TransferBatchInput,TransferDecisionInput
from . import transfer_service as service
router=APIRouter()
@router.get('/complaints/{id}/transfer-candidates')
def candidates(id:int,db=Depends(session),u=Depends(current)):return service.candidates(db,id)
@router.get('/complaints/{id}/transfers')
def transfers(id:int,db=Depends(session),u=Depends(current)):return service.batches_for(db,id)
@router.post('/complaints/{id}/transfers')
def create(id:int,data:TransferBatchInput,db=Depends(session),u=Depends(supervisor)):return service.batch_detail(db,service.create_batch(db,u,id,data))
@router.get('/transfers/{id}')
def detail(id:int,db=Depends(session),u=Depends(current)):return service.batch_detail(db,id)
@router.post('/transfers/{id}/decide')
def decide(id:int,data:TransferDecisionInput,db=Depends(session),u=Depends(supervisor)):return service.batch_detail(db,service.decide(db,u,id,data))
@router.post('/transfers/{id}/execute')
def execute(id:int,db=Depends(session),u=Depends(operator)):return service.batch_detail(db,service.execute(db,u,id))
