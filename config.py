from pathlib import Path
from dotenv import load_dotenv
load_dotenv(
    dotenv_path=Path(__file__).resolve().parent / ".env",
    override=True,
)
MEDIA_UPLOAD_EXTEND_MINUTES = 360   #每个媒体增加六小时(360min)
PHOTO_UPLOAD_EXTEND_MINUTES = 60
VIDEO_UPLOAD_EXTEND_MINUTES = 360
OTHERS_UPLOAD_EXTEND_MINUTES = 360
MEDIA_VIEW_COST_MINUTES = 60
MESSAGE_EXTEND_MINUTES = 60  #60
REWARD_HOURS_PER_MEDIA = 1
MAX_VALID_DURATION_MINUTES = 4320	#三天
INACTIVE_EXPIRE_DAYS = 7  # 通行证过期超过此天数后，可由管理员清理


