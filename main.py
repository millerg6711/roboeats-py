import asyncio
import datetime
import json
import os
import uuid

import aioschedule as schedule
import httpx
from google.cloud import firestore
from teslapy import Tesla, Vehicle

from api.delivery.uber_supplier_client import UberSupplierClient
from db import TRIPS


class UberDriverClient(httpx.AsyncClient):
    BASE_URL = os.getenv('BASE_URL')

    TOKEN_ENDPOINT = os.getenv('TOKEN_ENDPOINT')
    OFFERS_ENDPOINT = os.getenv('OFFER_ENDPOINT')

    CLIENT_ID = os.getenv('CLIENT_ID')
    REFRESH_TOKEN = os.getenv('REFRESH_TOKEN')

    TOKEN_EXPIRATION_TIME = None

    def __init__(self, **attrs):
        super().__init__(base_url=self.BASE_URL, headers={
            'Accept': 'application/json'
        }, event_hooks={'request': [self.req_interceptor]}, **attrs)

    async def req_interceptor(self, request: httpx.Request):
        if self.TOKEN_ENDPOINT not in str(request.url):
            if not self.TOKEN_EXPIRATION_TIME or datetime.datetime.now() > self.TOKEN_EXPIRATION_TIME:
                await self.refresh_token(request)

    async def refresh_token(self, request: httpx.Request) -> None:
        try:
            resp = await self.post(self.TOKEN_ENDPOINT, json={
                'request': {
                    'clientID': self.CLIENT_ID,
                    'refreshToken': self.REFRESH_TOKEN,
                    'grantType': 'REFRESH_TOKEN'
                }
            })
            print(resp.content)
            resp.raise_for_status()
            data = resp.json()

            self.headers['Authorization'] = f'Bearer {data["accessToken"]}'
            self.TOKEN_EXPIRATION_TIME = datetime.datetime.now() + datetime.timedelta(seconds=data['expiresIn']['low'])
            request.headers['Authorization'] = self.headers['Authorization']

        except httpx.RequestError as e:
            raise Exception('Failed to authenticate', e)

    async def get_offers(self, event: dict) -> list:
        headers = {'X-Uber-Device-Location-Latitude': str(event['lat']),
                   'X-Uber-Device-Location-Longitude': str(event['long'])}
        resp = await self.get(self.OFFERS_ENDPOINT, headers=headers)
        print(resp.content)
        data = resp.json()
        return data['data']['offers']


def get_elem_label_text(clusters: list, search_str: str) -> str:
    for cluster in clusters:
        for elem in cluster['elements']:
            if 'label' in elem:
                elem_label_text = elem['label']['text']['text']
                if search_str in elem_label_text:
                    return elem_label_text
    return ''


def get_display_text(location: dict) -> str:
    if 'title' in location and 'subtitle' in location:
        return f'{location["title"]}, {location["subtitle"]}'

    return f'{location["latitude"]}, {location["longitude"]}'


async def get_trip(offer: dict, event: dict) -> dict:
    offer_uuid, *_ = offer['offerUUIDs']

    primary_offer = offer['driverOfferData']['primaryOffer']
    job_offer_model = primary_offer['metaData']['jobOfferModel']
    job_offer_view = primary_offer['offerView']['jobOfferViewV3']
    payment = get_elem_label_text(job_offer_view['coreInfo']['defaultView']['clusters'], '$')
    distance = get_elem_label_text(job_offer_view['clusters'], 'total')

    start_location_ref = job_offer_model['startLocationRef']
    location_map = job_offer_model['locationMap']

    end_location_ref = job_offer_model['endLocationRef']
    via_locations_ref = job_offer_model['viaLocationRefs'] or []

    # add start location
    valid_locations = [start_location_ref]

    # add extra locations
    valid_locations.extend(via_locations_ref)

    # add end location
    valid_locations.append(end_location_ref)

    route = {k: v for k, v in location_map.items() if k in valid_locations} or location_map

    unique_trips = {}

    for k, v in route.items():
        location = next((v2 for k2, v2 in location_map.items()
                         if v['latitude'] == v2['latitude']
                         and v['longitude'] == v2['longitude']
                         and 'title' in v2), v)

        location['id'] = k
        location['order'] = valid_locations.index(k) if k in valid_locations else -1
        location['text'] = get_display_text(location)
        location['name'] = location['title'] if 'title' in location else location['text']
        location['maps'] = f'https://maps.google.com/?q={location["latitude"]},{location["longitude"]}'
        location['coordinates'] = firestore.GeoPoint(location["latitude"], location["longitude"])

        unique_trips[f'{location["latitude"]},f{location["longitude"]}'] = location

    locations = list(unique_trips.values())

    locations.sort(key=lambda x: x['order'])

    return {
        'created': datetime.datetime.now(),
        'offer_uuid': offer_uuid,
        'job_uuid': primary_offer['jobUUID'],
        'locations': locations,
        'expires': job_offer_model['expiresAtEpochMS'],
        'payment': payment,
        'distance': distance,
        'completed': False,
        'accepted': False
    }


async def get_offers(vehicle: Vehicle, driver: UberDriverClient, supplier_client: UberSupplierClient):
    print()
    # get vehicle data
    vehicle_data = vehicle.get_vehicle_data()

    # get vehicle & drive state
    drive_state = vehicle_data['drive_state']
    print(json.dumps(drive_state).encode('utf8'))

    # get event
    event = await supplier_client.get_event(drive_state)

    # get driver offers
    driver_offers = await driver.get_offers(event)
    for offer in driver_offers:
        if 'primaryOffer' in offer['driverOfferData']:
            trip = await get_trip(offer, event)
            TRIPS.document(trip['offer_uuid']).set(trip, merge=True)


async def send_calendar_events(vehicle: Vehicle):
    events = []

    query = TRIPS.order_by(u'created', direction=firestore.Query.DESCENDING) \
        .limit(2)

    for doc in query.stream():
        data = doc.to_dict()

        event = {
            'all_day': True,
            'name': f'{data["payment"]} | {data["distance"]}'
        }

        events.append(event)

        for location in data['locations']:
            name = [location['name']]

            events.append({
                'all_day': True,
                'name': ' , '.join(name),
                'location': location['text']
            })

    # sort events by incrementing start time
    for i in range(len(events)):
        events[i]['start'] = int((datetime.datetime.now() + datetime.timedelta(minutes=i)).timestamp()) * 1000

    print(json.dumps(events))

    vehicle.sync_wake_up()
    vehicle.api('CALENDAR_SYNC', calendar_data={
        'access_disabled': False,
        'calendars': [
            {
                'events': events
            }
        ],
        'phone_name': 'iPhone',
        'uuid': str(uuid.uuid4())
    })


async def main():
    with Tesla(os.getenv('TESLA_EMAIL')) as tesla:
        vehicle, *_ = tesla.vehicle_list()
        vehicle.sync_wake_up()

        async with UberDriverClient() as driver_client, UberSupplierClient() as supplier_client:
            # send calendar events
            schedule.every(3).seconds.do(send_calendar_events, vehicle)

            # get offers
            schedule.every(2).seconds.do(get_offers, vehicle, driver_client, supplier_client)

            while True:
                await schedule.run_pending()


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
