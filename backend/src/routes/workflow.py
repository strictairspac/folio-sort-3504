from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import (CaseForm, LoginForm, ReviewForm,
                                  TransferForm)
from src.services import domain, workflow
from src.services.security import current_user, login, operator, reviewer
from src.services.transfers import TRANSFER_SPEC, TransferError

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/transfer-spec')
def transfer_spec(_=Depends(current_user)):
    return TRANSFER_SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/transfers')
def list_transfer_jobs(_=Depends(current_user)):
    return workflow.list_transfers()


@router.get('/transfers/{identity}')
def get_transfer_job(identity: int, _=Depends(current_user)):
    return workflow.get_transfer(identity)


@router.post('/transfers', status_code=202)
def create_transfer(form: TransferForm, actor=Depends(operator)):
    try:
        row, duplicated = workflow.enqueue_transfer(form.payload, actor)
    except TransferError as exc:
        # 领域校验错误（件号不存在、批次相同等）退回工作台，不产生交接记录。
        raise HTTPException(422, str(exc)) from None
    # 同交接号重试：返回 200 与原记录，明确告知没有重复转件。
    if duplicated:
        return JSONResponse(row, status_code=status.HTTP_200_OK,
                            headers={'X-Idempotent-Replay': 'true'})
    return JSONResponse(row, status_code=status.HTTP_202_ACCEPTED)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
