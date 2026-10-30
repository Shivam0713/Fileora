from pymongo import MongoClient, ASCENDING, DESCENDING
from app.core.config import settings

client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=2500)
db = client[settings.mongodb_database]

def ensure_indexes():
    # The worker must remove the disk blob before deleting its metadata. A Mongo
    # TTL index could delete the metadata first and leave an orphaned file.
    for index in db.files.list_indexes():
        if index.get("name") == "expires_at_1" and "expireAfterSeconds" in index:
            db.files.drop_index("expires_at_1")
    db.files.create_index([("expires_at", ASCENDING)])
    db.files.create_index([("owner", ASCENDING), ("created_at", DESCENDING)])
    db.jobs.create_index([("owner", ASCENDING), ("created_at", DESCENDING)])
    db.jobs.create_index([("file_id", ASCENDING)])
