import logging
import random
from urllib import parse
from uuid import uuid4

from aiohttp.web import Request, Response, HTTPFound
from aiohttp.web_exceptions import HTTPNotFound
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import ATTR_MEDIA_SHUFFLE, \
    ATTR_MEDIA_CONTENT_TYPE, ATTR_MEDIA_CONTENT_ID, SERVICE_PLAY_MEDIA, \
    DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import ServiceCall
from homeassistant.helpers.network import get_url, NoURLAvailableError
from homeassistant.helpers.typing import HomeAssistantType
from youtube_dl import YoutubeDL

from pychromecast.controllers.media import MediaController

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'media_extractor'
QUEUE_SYMB = ','
ATTR_LIST_LIMIT = 'list_limit'


def chromecast_monkey_patch():
    """Add support for playing queues with Chromecast."""

    _LOGGER.debug("Apply Chromecast monkey patch")

    _send_start_play_media = MediaController._send_start_play_media

    def monkey(self, url: str, *args, **kwargs):
        if QUEUE_SYMB in url:
            self.send_message({
                'type': 'QUEUE_LOAD',
                'items': [{'media': {'contentId': p}}
                          for p in url.split(QUEUE_SYMB)]
            }, inc_session_id=True)
        else:
            _send_start_play_media(self, url, *args, **kwargs)

    MediaController._send_start_play_media = monkey


def setup(hass: HomeAssistantType, hass_config):
    try:
        base_url = get_url(hass) + '/api/media_extractor?'
        _LOGGER.debug(f"Media URL: {base_url}")
    except NoURLAvailableError:
        _LOGGER.error("Can't get hass URL")
        return False

    chromecast_monkey_patch()

    formats = hass_config[DOMAIN].get('customize', {})
    def_format = hass_config[DOMAIN].get('default_query', 'best')

    # token protects from evil queries
    token = str(uuid4())

    def process_url(media: dict, format_: str):
        """Generate url to process direct link to media"""
        return base_url + parse.urlencode({
            'ie_key': media['ie_key'] if media['ie_key'] else '',
            'url': media['url'],
            'format': format_,
            'token': token
        })

    ydl = YoutubeDL({"quiet": True, "logger": _LOGGER})

    def play_media(call: ServiceCall):
        """Main play_media service"""
        entity_id = call.data.get(ATTR_ENTITY_ID)
        content_type = call.data.get(ATTR_MEDIA_CONTENT_TYPE)
        url = call.data.get(ATTR_MEDIA_CONTENT_ID)
        shuffle = call.data.get(ATTR_MEDIA_SHUFFLE)
        limit = call.data.get(ATTR_LIST_LIMIT)

        custom = formats.get(entity_id[0], {})

        _LOGGER.debug(f"Extract {url}")

        media = ydl.extract_info(url, download=False, process=False)
        if not media:
            _LOGGER.warning("Can't extract media info")
            return

        elif 'entries' in media:
            format_ = custom.get(content_type, def_format)
            media = [process_url(p, format_) for p in media['entries']]
            if shuffle:
                random.shuffle(media)
            if limit > 0:
                media = media[:limit]
            url = QUEUE_SYMB.join(media)

        else:
            ydl.params['format'] = custom.get(content_type, def_format)
            media = ydl.process_ie_result(media, download=False)
            url = media['url']

        _LOGGER.debug(f"Play {url}")

        hass.async_create_task(hass.services.async_call(
            MEDIA_PLAYER_DOMAIN, SERVICE_PLAY_MEDIA, {
                ATTR_MEDIA_CONTENT_ID: url,
                ATTR_ENTITY_ID: entity_id,
                ATTR_MEDIA_CONTENT_TYPE: content_type
            }
        ))

    hass.services.register(DOMAIN, SERVICE_PLAY_MEDIA, play_media)

    hass.http.register_view(MediaProcessView(ydl, token, hass))

    return True


class MediaProcessView(HomeAssistantView):
    url = '/api/media_extractor'
    name = 'api:media_extractor'
    requires_auth = False

    def __init__(self, ydl: YoutubeDL, token: str, hass):
        self.ydl = ydl
        self.token = token
        self.hass = hass

    async def get(self, request: Request) -> Response:
        if request.query.get('token') != self.token:
            return HTTPNotFound()

        self.ydl.params['format'] = request.query['format']
        media = await self.hass.async_add_executor_job(
            self.ydl.process_ie_result, {
                '_type': 'url',
                'ie_key': request.query['ie_key'] or None,
                'url': request.query['url']
            }, False)

        _LOGGER.debug(f"Redirect to {media['url']}")

        return HTTPFound(media['url'])
