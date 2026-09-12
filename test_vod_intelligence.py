"""Test script for vod_intelligence package."""
from vod_intelligence.config import ScoringWeights

w = ScoringWeights()
total = sum(v for k, v in w.__dict__.items() if not k.startswith("_"))
print(f"Total weight: {total}")
assert abs(total - 1.0) < 0.01, f"Weights must sum to 1.0, got {total}"
print("Config validation OK")

# Test job manager
from vod_intelligence import JobManager, VODJob, JobState
jm = JobManager()
job = jm.create_job("https://kick.com/test/videos/123", 12345, 67890)
print(f"Created job: {job.id}")
assert job.state == JobState.CREATED
print("JobManager OK")

# Test URL validation
from vod_intelligence import is_kick_vod_url, extract_streamer_from_vod_url
assert is_kick_vod_url("https://kick.com/user/videos/123") == True
assert is_kick_vod_url("https://kick.com/user/clip/123") == False
assert extract_streamer_from_vod_url("https://kick.com/username/videos/123") == "username"
print("URL validation OK")

print("\n[OK] All basic tests passed!")