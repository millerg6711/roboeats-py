import json
import os

import httpx


class UberSupplierClient(httpx.AsyncClient):
    SUPPLIER_BASE_URL = os.getenv('SUPPLIER_BASE_URL')

    GRAPHQL_ENDPOINT = '/graphql'

    UBER_SUPPLIER_SID = os.getenv('UBER_SUPPLIER_SID')
    UBER_SUPPLIER_CSID = os.getenv('UBER_SUPPLIER_CSID')

    DRIVER_UUID = os.getenv('DRIVER_UUID')

    def __init__(self, **attrs):
        super().__init__(base_url=self.SUPPLIER_BASE_URL, headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Cookie': f'sid={self.UBER_SUPPLIER_SID};csid={self.UBER_SUPPLIER_CSID};',
            'x-csrf-token': 'x'
        }, **attrs)

    async def get_event(self, drive_state) -> dict | None:
        event = {'status': None, 'lat': drive_state['latitude'], 'long': drive_state['longitude']}

        resp = await self.post(self.GRAPHQL_ENDPOINT, json={
            'query': """query GetDriverEvents($orgUUID: String!) {
                              getDriverEvents(orgUUID: $orgUUID) {
                                driverEvents {
                                  driverUUID
                                  driverStatus
                                  driverLocation {
                                    latitude
                                    longitude
                                    course
                                    __typename
                                  }
                                  dropOffInfo {
                                    locations {
                                      latitude
                                      longitude
                                      __typename
                                    }
                                    __typename
                                  }
                                  driverStatusState
                                  vehicleUUID
                                  __typename
                                }
                                __typename
                              }
                            }""",
            'variables': json.dumps({
                'orgUUID': self.DRIVER_UUID
            })
        })
        print(resp.content)
        data = resp.json()

        driver_events = data['data']['getDriverEvents']['driverEvents']

        if driver_events:
            driver_event, *_ = driver_events
            event['status'] = driver_event['driverStatus']

            if 'driverLocation' in driver_event:
                driver_location = driver_event['driverLocation']
                event['lat'] = driver_location['latitude']
                event['long'] = driver_location['longitude']

        return event
