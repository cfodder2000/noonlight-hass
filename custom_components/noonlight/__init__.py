"""Noonlight integration for Home Assistant."""
from datetime import timedelta
import logging

import noonlight as nl
import voluptuous as vol

from homeassistant.const import (
    CONF_ID, CONF_LATITUDE, CONF_LONGITUDE, EVENT_HOMEASSISTANT_START)
from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.event import (
    async_track_point_in_utc_time, async_track_time_interval)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.util.dt as dt_util

DOMAIN = 'noonlight'

EVENT_NOONLIGHT_TOKEN_REFRESHED = 'noonlight_token_refreshed'
EVENT_NOONLIGHT_ALARM_CANCELED = 'noonlight_alarm_canceled'
EVENT_NOONLIGHT_ALARM_CREATED = 'noonlight_alarm_created'

NOTIFICATION_TOKEN_UPDATE_FAILURE = 'noonlight_token_update_failure'
NOTIFICATION_TOKEN_UPDATE_SUCCESS = 'noonlight_token_update_success'
NOTIFICATION_ALARM_CREATE_FAILURE = 'noonlight_alarm_create_failure'

TOKEN_CHECK_INTERVAL = timedelta(minutes=15)

CONF_SECRET = 'secret'
CONF_API_ENDPOINT = 'api_endpoint'
CONF_TOKEN_ENDPOINT = 'token_endpoint'
CONF_LINE1 = 'line1'
CONF_LINE2 = 'line2'
CONF_CITY = 'city'
CONF_STATE = 'state'
CONF_ZIP = 'zip'

CONST_ALARM_STATUS_ACTIVE = 'ACTIVE'
CONST_ALARM_STATUS_CANCELED = 'CANCELED'
CONST_NOONLIGHT_HA_SERVICE_CREATE_ALARM = 'create_alarm'
CONST_NOONLIGHT_SERVICE_TYPES = (
    nl.NOONLIGHT_SERVICES_POLICE,
    nl.NOONLIGHT_SERVICES_FIRE,
    nl.NOONLIGHT_SERVICES_MEDICAL
    )

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_ID): cv.string,
        vol.Required(CONF_SECRET): cv.string,
        vol.Required(CONF_API_ENDPOINT): cv.string,
        vol.Required(CONF_TOKEN_ENDPOINT): cv.string,
        vol.Optional(CONF_LINE1): cv.string,
        vol.Optional(CONF_LINE2): cv.string,
        vol.Optional(CONF_CITY): cv.string,
        vol.Optional(CONF_STATE): cv.string,
        vol.Optional(CONF_ZIP): cv.string,
        vol.Inclusive(CONF_LATITUDE, 'coordinates',
                      'Include both latitude and longitude'): cv.latitude,
        vol.Inclusive(CONF_LONGITUDE, 'coordinates',
                      'Include both latitude and longitude'): cv.longitude,
    })
}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass, config):
    """Set up integration."""
    conf = config[DOMAIN]

    noonlight_integration = NoonlightIntegration(hass, conf)
    hass.data[DOMAIN] = noonlight_integration

    async def handle_create_alarm_service(call):
        """Create a noonlight alarm from a service"""
        service = call.data.get('service', None)
        await noonlight_integration.create_alarm(alarm_types=[service])

    hass.services.async_register(DOMAIN, 
        CONST_NOONLIGHT_HA_SERVICE_CREATE_ALARM, handle_create_alarm_service)

    async def check_api_token(now):
        """Check if the current API token has expired and renew if so."""
        next_check_interval = TOKEN_CHECK_INTERVAL

        result = await noonlight_integration.check_api_token()

        if not result:
            _LOGGER.error("API token failed renewal, retrying in 3 min")
            check_api_token.fail_count += 1
            persistent_notification.create(
                hass,
                "Noonlight API token failed to renew {} time{}!\n"
                "Home Assistant will automatically attempt to renew the "
                "API token in 3 minutes.".format(
                    check_api_token.fail_count,
                    's' if check_api_token.fail_count > 1 else ''
                    ),
                "Noonlight Token Renewal Failure",
                NOTIFICATION_TOKEN_UPDATE_FAILURE)
            next_check_interval = timedelta(minutes=3)
        else:
            if check_api_token.fail_count > 0:
                persistent_notification.create(
                    hass,
                    "Noonlight API token has now been "
                    "renewed successfully.",
                    "Noonlight Token Renewal Success",
                    NOTIFICATION_TOKEN_UPDATE_SUCCESS)
            check_api_token.fail_count = 0

        async_track_point_in_utc_time(
            hass, check_api_token, dt_util.utcnow() + next_check_interval)

    check_api_token.fail_count = 0

    @callback
    def schedule_first_token_check(event):
        """Schedule the first token renewal when Home Assistant starts up."""
        async_track_point_in_utc_time(hass, check_api_token, dt_util.utcnow())

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START,
                               schedule_first_token_check)

    hass.async_create_task(
        async_load_platform(hass, 'switch', DOMAIN, {}, config))

    return True


class NoonlightException(HomeAssistantError):
    """General exception for Noonlight Integration."""

    pass


class NoonlightIntegration():
    """Integration for interacting with Noonlight from Home Assistant."""

    def __init__(self, hass, conf):
        """Initialize NoonlightIntegration."""
        self.hass = hass
        self.config = conf
        self._access_token_response = {}
        self._alarm = None
        self._time_to_renew = timedelta(hours=2)
        self._websession = async_get_clientsession(self.hass)
        self.client = nl.NoonlightClient(token=self.access_token,
                                         session=self._websession)
        self.client.set_base_url(self.config[CONF_API_ENDPOINT])
        
        #Add address portions, if exist
        self.addline1 = self.config.get(CONF_LINE1,'')
        self.addline2 = self.config.get(CONF_LINE2,'')
        self.addcity = self.config.get(CONF_CITY,'')
        self.addstate = self.config.get(CONF_STATE,'')
        self.addzip = self.config.get(CONF_ZIP,'')

    @property
    def latitude(self):
        """Return latitude from the Home Assistant configuration."""
        return self.config \
            .get(CONF_LATITUDE, self.hass.config.latitude)

    @property
    def longitude(self):
        """Return longitude from the Home Assistant configuration."""
        return self.config \
            .get(CONF_LONGITUDE, self.hass.config.longitude)

    @property
    def access_token(self):
        """Return the access token from the Noonlight Configuration."""
        return self._access_token_response \
            .get('token')

    @property
    def access_token_expiry(self):
        """Return the timestamp when the access token expires."""
        return self._access_token_response \
            .get('expires', dt_util.utc_from_timestamp(0))

    @property
    def access_token_expires_in(self):
        """Will return the timedelta when the token expires."""
        return self.access_token_expiry - dt_util.utcnow()

    @property
    def should_token_be_renewed(self):
        """Will return true if the token needs to be renewed."""
        return self.access_token is None \
            or self.access_token_expires_in <= self._time_to_renew

    async def check_api_token(self, force_renew=False):
        """Check if Noonlight API token needs renewal and renew if so."""
        _LOGGER.debug("Checking if token needs renewal, expires: {0:.1f}h"
                      .format(self.access_token_expires_in
                              .total_seconds() / 3600.0))
        if self.should_token_be_renewed or force_renew:
            try:
                _LOGGER.debug("Renewing Noonlight access token")
                path = self.config.get(CONF_TOKEN_ENDPOINT)
                data = {
                    'id': self.config.get(CONF_ID),
                    'secret': self.config.get(CONF_SECRET)
                }
                headers = {'Content-Type': 'application/json'}
                token_response = {}
                async with self._websession.post(
                        path, json=data, headers=headers) as resp:
                    token_response = await resp.json()
                if 'token' in token_response and 'expires' in token_response:
                    self._set_token_response(token_response)
                    _LOGGER.debug("Token set: {}".format(self.access_token))
                    _LOGGER.debug("Token renewed, expires at {0} ({1:.1f}h)"
                                  .format(self.access_token_expiry,
                                          self.access_token_expires_in
                                          .total_seconds()/3600.0))
                    self.hass.helpers.dispatcher.async_dispatcher_send(
                        EVENT_NOONLIGHT_TOKEN_REFRESHED)
                    return True
                raise NoonlightException("unexpected token_response: {}"
                                         .format(token_response))
            except NoonlightException:
                _LOGGER.exception("Failed to renew Noonlight token")
                return False
        return True

    def _set_token_response(self, token_response):
        expires = dt_util.parse_datetime(token_response['expires'])
        if expires is not None:
            token_response['expires'] = expires
        else:
            token_response['expires'] = dt_util.utc_from_timestamp(0)
        self.client.set_token(token=token_response.get('token'))
        self._access_token_response = token_response

    async def update_alarm_status(self):
        """Update the status of the current alarm."""
        if self._alarm is not None:
            return await self._alarm.get_status()

    async def create_alarm(self, alarm_types=[nl.NOONLIGHT_SERVICES_POLICE]):
        """Create a new alarm"""
        services = {}
        for alarm_type in alarm_types or ():
            if alarm_type in CONST_NOONLIGHT_SERVICE_TYPES:
                services[alarm_type] = True
        if self._alarm is None:
            try:
                if len(self.addline1) > 0:
                    alarm_body = {
                        'location.address': {
                            'line1': self.addline1,
                            'city': self.addcity,
                            'state': self.addstate,
                            'zip': self.addzip
                        }
                    }
                    if len(self.addline2) > 0:
                        alarm_body['location.address']['line2'] = self.addline2
                else:
                    alarm_body = {
                        'location.coordinates': {
                            'lat': self.latitude,
                            'lng': self.longitude,
                            'accuracy': 5
                        }
                    }
                if len(services) > 0:
                    alarm_body['services'] = services
                self._alarm = await self.client.create_alarm(
                    body=alarm_body
                )
            except nl.NoonlightClient.ClientError as client_error:
                persistent_notification.create(
                    self.hass,
                    "Failed to send an alarm to Noonlight!\n\n"
                    "({}: {})".format(type(client_error).__name__,
                                      str(client_error)),
                    "Noonlight Alarm Failure",
                    NOTIFICATION_ALARM_CREATE_FAILURE)
            if self._alarm and self._alarm.status == CONST_ALARM_STATUS_ACTIVE:
                self.hass.helpers.dispatcher.async_dispatcher_send(
                    EVENT_NOONLIGHT_ALARM_CREATED)
                _LOGGER.debug(
                    'noonlight alarm has been initiated. '
                    'id: %s status: %s',
                    self._alarm.id,
                    self._alarm.status)
                cancel_interval = None

                async def check_alarm_status_interval(now):
                    _LOGGER.debug('checking alarm status...')
                    if await self.update_alarm_status() == \
                            CONST_ALARM_STATUS_CANCELED:
                        _LOGGER.debug(
                            'alarm %s has been canceled!',
                            self._alarm.id)
                        if cancel_interval is not None:
                            cancel_interval()
                        if self._alarm is not None:
                            if self._alarm.status == \
                                CONST_ALARM_STATUS_CANCELED:
                                self._alarm = None
                        self.hass.helpers.dispatcher.async_dispatcher_send(
                            EVENT_NOONLIGHT_ALARM_CANCELED)
                cancel_interval = async_track_time_interval(
                    self.hass,
                    check_alarm_status_interval,
                    timedelta(seconds=15)
                    )
