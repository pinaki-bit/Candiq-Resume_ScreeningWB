import logging
import boto3
from botocore.exceptions import ClientError
from app.config import get_settings

logger = logging.getLogger(__name__)

def _get_s3_client():
    settings = get_settings()
    if not settings.use_s3_storage:
        return None
        
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        endpoint_url=settings.s3_endpoint_url
    )

def upload_file(content: bytes, filename: str, content_type: str = "application/pdf") -> str | None:
    """
    Upload a file to S3.
    Returns the S3 URL if successful, or None if S3 is not configured/failed.
    """
    settings = get_settings()
    s3_client = _get_s3_client()
    
    if not s3_client or not settings.aws_s3_bucket:
        return None

    try:
        s3_client.put_object(
            Bucket=settings.aws_s3_bucket,
            Key=filename,
            Body=content,
            ContentType=content_type
        )
        
        # Determine the endpoint format
        if settings.s3_endpoint_url:
            # E.g. Cloudflare R2 / Custom Endpoint
            return f"{settings.s3_endpoint_url.rstrip('/')}/{settings.aws_s3_bucket}/{filename}"
        else:
            # Default AWS S3 format
            return f"https://{settings.aws_s3_bucket}.s3.{settings.aws_region}.amazonaws.com/{filename}"
            
    except ClientError as e:
        logger.error(f"Failed to upload {filename} to S3: {e}")
        return None

def delete_file(filename: str) -> bool:
    """
    Delete a file from S3.
    Returns True if successful, False otherwise.
    """
    settings = get_settings()
    s3_client = _get_s3_client()
    
    if not s3_client or not settings.aws_s3_bucket:
        return False

    try:
        s3_client.delete_object(
            Bucket=settings.aws_s3_bucket,
            Key=filename
        )
        return True
    except ClientError as e:
        logger.error(f"Failed to delete {filename} from S3: {e}")
        return False
