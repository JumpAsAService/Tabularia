from app.utils import StorageService

storage = StorageService()

print(storage.create_bucket('test'))
print(storage.list_objects(bucket='test'))