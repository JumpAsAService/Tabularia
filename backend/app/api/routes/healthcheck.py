from fastapi import APIRouter, FastAPI, Depends, HTTPException, status
from fastapi.responses import JSONResponse
import redis
from app.core.config import get_settings
from app.utils import get_storage_service


router = APIRouter(
    prefix="/healthcheck",
    tags=["health"]
)

settings = get_settings()

@router.get('/', status_code=status.HTTP_200_OK, tags=["health"])
def health_check():
    """
    Health check for the application
    """
    return JSONResponse(content={"status": "healthy"}, status_code=status.HTTP_200_OK) 

@router.get('/redis', status_code=status.HTTP_200_OK, tags=["health"])
def redis_health():
    """
    Health check for the cache Redis service
    """
    try:
        r = redis.Redis(
            host=settings.redis.host,
            port=settings.redis.port, 
            db=settings.redis.db,
            password=settings.redis.password,
            decode_responses=True)
        r.ping()

        return JSONResponse(content="Redis is healthy", status_code=status.HTTP_200_OK)
    except redis.ConnectionError:
        return JSONResponse(content="Redis is not reachable", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)  # noqa: E501

@router.get('/s3', status_code=status.HTTP_200_OK, tags=["health"])
def s3_health():
    """
    Health check for object storage service
    """
    try:
        s = get_storage_service()
        s.create_bucket(bucket='test')
        s.bucket_exists(bucket='test')
        return JSONResponse(content="S3 is healthy", status_code=status.HTTP_200_OK)
    except redis.ConnectionError:
        return JSONResponse(content="S3 is not reachable", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)  # noqa: E501
