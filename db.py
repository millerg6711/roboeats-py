import firebase_admin
from firebase_admin import firestore, credentials
from google.cloud.firestore_v1.base_collection import BaseCollectionReference

firebase_admin.initialize_app(credentials.Certificate("serviceAccountKey.json"))
DB = firestore.client()

TRIPS: BaseCollectionReference = DB.collection(u'trips')
