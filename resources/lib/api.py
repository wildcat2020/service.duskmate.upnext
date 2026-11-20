# -*- coding: utf-8 -*-
# GNU General Public License v2.0 (see COPYING or https://www.gnu.org/licenses/gpl-2.0.txt)

import json
from uuid import uuid4

from xbmc import PLAYLIST_VIDEO, PLAYLIST_MUSIC, executebuiltin, getCondVisibility
from utils import (binge_mode, clear_property, event, get_int, get_property, get_setting_bool,
                   get_setting_int, jsonrpc, log as ulog, wait_for_abort)


# Binge mode moves on this many seconds into the end credits when the setting is 0
BINGE_CREDITS_DELAY = 10

# Episode details shown in the popup
EPISODE_PROPERTIES = ['art', 'dateadded', 'episode', 'file', 'firstaired', 'lastplayed',
                      'playcount', 'plot', 'rating', 'resume', 'runtime', 'season',
                      'showtitle', 'streamdetails', 'title', 'tvshowid', 'writer']


def _log(msg, level=2):
    """Log an API message without expanding Api's public interface."""
    ulog(msg, name='Api', level=level)


def credits_autoplay_deadline(data, play_time, total_time):
    """Return an opt-in playback deadline only when a credits offset is known."""
    delay = get_setting_int('autoPlayCreditsTime')
    if delay <= 0 and binge_mode():
        delay = BINGE_CREDITS_DELAY
    if delay <= 0:
        return None
    try:
        offset = float(data.get('notification_offset'))
    except (TypeError, ValueError):
        return None
    if not 0 < offset < total_time:
        return None
    # An earlier notification_time may show the prompt before the credits.
    # A late prompt still gives the viewer the full configured response time.
    return min(total_time, max(play_time, offset) + delay)


def get_watched_state(episodeid):
    """Return (play count, resume position) of a library episode, or None if it isn't in the library"""
    result = jsonrpc(method='VideoLibrary.GetEpisodeDetails', params={
        'episodeid': episodeid, 'properties': ['playcount', 'resume'],
    })
    details = (result.get('result') or {}).get('episodedetails')
    if not details:
        return None
    return details.get('playcount') or 0, (details.get('resume') or {}).get('position') or 0


def playing_episode_id():
    """Return the library ID of the playing episode, or None if no library episode is playing"""
    players = jsonrpc(method='Player.GetActivePlayers').get('result') or []
    video = [player.get('playerid') for player in players if player.get('type') == 'video']
    if not video:
        return None
    item = (jsonrpc(method='Player.GetItem', params={'playerid': video[0]}).get('result') or {}).get('item') or {}
    return item.get('id') if item.get('type') == 'episode' else None


def playlist_position():
    """Return the playing video's position in its playlist: -1 when Kodi didn't play it from one (an
    add-on opened it with Player.Open, for example), None when there is no answer"""
    playerid = Api._get_playerid(playerid_cache=[None])  # pylint: disable=protected-access
    if playerid is None:
        return None
    result = jsonrpc(method='Player.GetProperties', params={'playerid': playerid, 'properties': ['position']})
    return get_int(result.get('result'), 'position', None)


def play_queued_item():
    """Open the item Up Next queued, the playlist's last, directly"""
    playlistid = Api.get_playlistid()
    if playlistid < 0:
        playlistid = Api.PLAYER_PLAYLIST['video']
    result = jsonrpc(method='Playlist.GetItems', params={'playlistid': playlistid})
    items = (result.get('result') or {}).get('items') or []
    if not items:
        return False
    jsonrpc(method='Player.Open', params={'item': {'playlistid': playlistid, 'position': len(items) - 1}})
    return True


def mark_watched(episodeid, playcount):
    """Set a library episode's play count and clear its resume point"""
    jsonrpc(method='VideoLibrary.SetEpisodeDetails', params={
        'episodeid': episodeid, 'playcount': playcount, 'resume': {'position': 0, 'total': 0},
    })


class Api:
    """Main API class"""
    _shared_state = {}

    PLAYER_PLAYLIST = {
        'video': PLAYLIST_VIDEO,  # 1
        'audio': PLAYLIST_MUSIC   # 0
    }

    def __init__(self):
        """Constructor for Api class"""
        self.__dict__ = self._shared_state
        self.data = {}
        self.encoding = 'base64'

    def has_addon_data(self):
        return self.data

    def reset_addon_data(self):
        self.data = {}

    @staticmethod
    def _tvshow_tmdb_id(tvshowid):
        if get_int({'id': tvshowid}, 'id', -1) < 0:
            return None
        result = jsonrpc(method='VideoLibrary.GetTVShows', params={'properties': ['uniqueid']})
        details = next((
            show for show in (result.get('result') or {}).get('tvshows', [])
            if get_int(show, 'tvshowid', -1) == int(tvshowid)
        ), {})
        return (details.get('uniqueid') or {}).get('tmdb')

    def resolve_duskmate_upnext(self, now_playing):
        """Ask Duskmate Helper to resolve the canonical next episode.

        The Helper owns TMDB, merged watched state and library imports. This service owns only
        the prompt and playback hand-off, so it does not duplicate provider or queue logic.
        """
        if not getCondVisibility('System.HasAddon(plugin.video.duskmate)'):
            return None
        item = (now_playing or {}).get('result', {}).get('item') or {}
        if item.get('type') != 'episode':
            return None
        season = get_int(item, 'season', -1)
        episode = get_int(item, 'episode', -1)
        if season < 0 or episode < 0:
            return None
        uniqueid = item.get('uniqueid') or {}
        tmdb_id = uniqueid.get('tvshow.tmdb') or self._tvshow_tmdb_id(item.get('tvshowid'))
        if not tmdb_id:
            _log('Duskmate Helper lookup skipped: current show has no TMDB series ID', 1)
            return None

        request_id = uuid4().hex
        property_name = 'Duskmate.UpNext.Response.%s' % request_id
        clear_property(property_name)
        executebuiltin(
            'RunScript(plugin.video.duskmate,duskmate_resolve_upnext,'
            'request_id=%s,tmdb_id=%s,season=%s,episode=%s)' % (
                request_id, tmdb_id, season, episode
            )
        )
        raw = ''
        # Resolution is metadata-only and normally cache-backed. Bound the wait so provider or
        # network failures never freeze the current playback or suppress the local fallback.
        for _ in range(150):
            raw = get_property(property_name)
            if raw:
                break
            if wait_for_abort(0.1):
                break
        clear_property(property_name)
        if not raw:
            _log('Duskmate Helper next-episode lookup timed out', 1)
            return None
        try:
            response = json.loads(raw)
        except (TypeError, ValueError):
            _log('Duskmate Helper returned invalid next-episode JSON', 1)
            return None
        next_episode = response.get('next_episode') if response.get('ok') else None
        play_url = response.get('play_url')
        if not next_episode or not str(play_url or '').startswith('plugin://plugin.video.duskmate/'):
            _log('Duskmate Helper found no playable next episode: %s' % response.get('reason'), 1)
            return None
        if next_episode.get('episodeid') is None:
            next_episode['episodeid'] = 'tmdb:%s:%s:%s' % (
                tmdb_id, next_episode.get('season'), next_episode.get('episode')
            )
        self.data = {
            'current_episode': {
                'episodeid': item.get('id'),
                'tvshowid': item.get('tvshowid'),
            },
            'next_episode': next_episode,
            'play_url': play_url,
            'resolved_by': 'plugin.video.duskmate',
        }
        return next_episode

    def addon_data_received(self, data, encoding='base64'):
        _log('addon_data_received called with data %s' % data, 2)
        self.data = data
        self.encoding = encoding

    @staticmethod
    def play_kodi_item(episode):
        jsonrpc(method='Player.Open', id=0, params={'item': {'episodeid': episode.get('episodeid')}})

    @staticmethod
    def _get_playerid(playerid_cache=[None]):  # pylint: disable=dangerous-default-value
        """Function to get active player playerid"""

        # We don't need to actually get playerid everytime, cache and reuse instead
        if playerid_cache[0] is not None:
            return playerid_cache[0]

        # Sometimes Kodi gets confused and uses a music playlist for video content,
        # so get the first active player instead, default to video player.
        result = jsonrpc(method='Player.GetActivePlayers')
        result = [
            player for player in result.get('result', [{}])
            if player.get('type', 'video') in Api.PLAYER_PLAYLIST
        ]

        playerid = get_int(result[0], 'playerid') if result else -1

        if playerid == -1:
            return None

        playerid_cache[0] = playerid
        return playerid

    @staticmethod
    def get_playlistid(playlistid_cache=[None]):  # pylint: disable=dangerous-default-value
        """Function to get playlistid of active player"""

        # We don't need to actually get playlistid everytime, cache and reuse instead
        if playlistid_cache[0] is not None:
            return playlistid_cache[0]

        result = jsonrpc(
            method='Player.GetProperties',
            params={
                'playerid': Api._get_playerid(playerid_cache=[None]),
                'properties': ['playlistid'],
            }
        )
        result = get_int(
            result.get('result', {}), 'playlistid', Api.PLAYER_PLAYLIST['video']
        )

        return result

    def queue_next_item(self, episode):
        next_item = {}
        if not self.data:
            next_item.update(episodeid=episode.get('episodeid'))
        elif self.data.get('play_url'):
            next_item.update(file=self.data.get('play_url'))

        if next_item:
            jsonrpc(
                method='Playlist.Add',
                id=0,
                params={
                    'playlistid': Api.get_playlistid(),
                    'item': next_item
                }
            )

        return bool(next_item)

    @staticmethod
    def dequeue_next_item():
        """Remove unplayed next item from video playlist"""
        jsonrpc(
            method='Playlist.Remove',
            id=0,
            params={
                'playlistid': Api.get_playlistid(),
                'position': 1
            }
        )
        return False

    @staticmethod
    def reset_queue():
        """Remove previously played item from video playlist"""
        jsonrpc(
            method='Playlist.Remove',
            id=0,
            params={
                'playlistid': Api.get_playlistid(),
                'position': 0
            }
        )

    def get_next_in_playlist(self, position):
        result = jsonrpc(method='Playlist.GetItems', params={
            'playlistid': Api.get_playlistid(),
            # limits are zero indexed, position is one indexed
            'limits': {'start': position, 'end': position + 1},
            'properties': EPISODE_PROPERTIES,
        })

        item = result.get('result', {}).get('items')

        # Don't check if next item is an episode, just use it if it is there
        if not item:  # item.get('type') != 'episode':
            _log('Error: no next item found in playlist', 1)
            return None
        item = item[0]

        # Playlist item may not have had video info details set
        # Try and populate required details if missing
        if not item.get('title'):
            item['title'] = item.get('label', '')
        item['episodeid'] = get_int(item, 'id')
        item['tvshowid'] = get_int(item, 'tvshowid')
        # If missing season/episode, change to empty string to avoid episode
        # formatting issues ("S-1E-1") in UpNext popup
        if get_int(item, 'season') == -1:
            item['season'] = ''
        if get_int(item, 'episode') == -1:
            item['episode'] = ''

        _log('Next item in playlist: %s' % item, 2)
        return item

    def play_addon_item(self):
        if self.data.get('play_url'):
            _log('Playing the next episode directly: %(play_url)s' % self.data, 2)
            jsonrpc(method='Player.Open', params={'item': {'file': self.data.get('play_url')}})
        else:
            message = 'Sending %(encoding)s data to add-on to play: %(play_info)s'
            _log(message % dict(encoding=self.encoding, **self.data), 2)  # pylint: disable=use-dict-literal
            event(message=self.data.get('id'), data=self.data.get('play_info'), sender='duskmateupnextprovider', encoding=self.encoding)

    def handle_addon_lookup_of_next_episode(self):
        if not self.data:
            return None
        _log('handle_addon_lookup_of_next_episode episode returning data %s' % self.data.get('next_episode'), 2)
        return self.data.get('next_episode')

    def handle_addon_lookup_of_current_episode(self):
        if not self.data:
            return None
        _log('handle_addon_lookup_of_current episode returning data %s' % self.data.get('current_episode'), 2)
        return self.data.get('current_episode')

    def notification_time(self, total_time=None):
        # Alway use metadata, when available
        if self.data.get('notification_time'):
            return int(self.data.get('notification_time'))

        # Some consumers send the offset when the credits start (e.g. Netflix)
        if total_time and self.data.get('notification_offset'):
            return total_time - int(self.data.get('notification_offset'))

        # Use a customized notification time, when configured
        if total_time and get_setting_bool('customAutoPlayTime'):
            if total_time > 60 * 60:
                return get_setting_int('autoPlayTimeXL')
            if total_time > 40 * 60:
                return get_setting_int('autoPlayTimeL')
            if total_time > 20 * 60:
                return get_setting_int('autoPlayTimeM')
            if total_time > 10 * 60:
                return get_setting_int('autoPlayTimeS')
            return get_setting_int('autoPlayTimeXS')

        # Use one global default, regardless of episode length
        return get_setting_int('autoPlaySeasonTime')

    def get_now_playing(self):
        # The player can take a moment to become active, but don't wait forever
        for _ in range(10):
            result = jsonrpc(method='Player.GetActivePlayers')
            if result.get('result'):
                break
            if wait_for_abort(0.1):
                return None
        else:
            _log('No active player found', 1)
            return None
        _log('Got active player %s' % result, 2)

        playerid = result.get('result')[0].get('playerid')

        # Get details of the playing media
        _log('Getting details of now playing media', 2)
        result = jsonrpc(method='Player.GetItem', params={
            'playerid': playerid,
            'properties': ['episode', 'genre', 'playcount', 'plotoutline', 'season', 'showtitle',
                           'tvshowid', 'uniqueid'],
        })
        _log('Got details of now playing media %s' % result, 2)
        return result

    def handle_kodi_lookup_of_episode(self, tvshowid, current_file, include_watched, current_episode_id):
        # Only fetch what's needed to choose, then the full details of the chosen episode
        result = jsonrpc(method='VideoLibrary.GetEpisodes', params={
            'tvshowid': tvshowid,
            'properties': ['episode', 'file', 'playcount', 'season'],
            'sort': {'method': 'episode'},
        })

        if not result.get('result'):
            return None

        _log('Got %d episodes of the show' % len(result.get('result', {}).get('episodes', [])), 2)

        # Find the next unwatched and the newest added episodes
        episode = self.find_next_episode(result, current_file, include_watched, current_episode_id)
        if not episode:
            return None
        return self.get_episode_details(episode.get('episodeid'))

    @staticmethod
    def get_episode_details(episodeid):
        result = jsonrpc(method='VideoLibrary.GetEpisodeDetails', params={
            'episodeid': episodeid,
            'properties': EPISODE_PROPERTIES,
        })
        return result.get('result', {}).get('episodedetails')

    @staticmethod
    def showtitle_to_id(title):
        result = jsonrpc(method='VideoLibrary.GetTVShows', id='libTvShows', params={'properties': ['title']})

        for tvshow in result.get('result', {}).get('tvshows', []):
            if tvshow.get('label') == title:
                return tvshow.get('tvshowid')
        return '-1'

    @staticmethod
    def get_episode_id(showid, show_season, show_episode):
        show_season = int(show_season)
        show_episode = int(show_episode)
        result = jsonrpc(method='VideoLibrary.GetEpisodes', params={
            'properties': ['episode', 'season'],
            'tvshowid': int(showid),
            'season': show_season,
        })

        episodeid = 0
        for episode in result.get('result', {}).get('episodes', []):
            if episode.get('episodeid') and episode.get('season') == show_season and episode.get('episode') == show_episode:
                episodeid = episode.get('episodeid')

        return episodeid

    def find_next_episode(self, result, current_file, include_watched, current_episode_id):
        found_match = False
        current_library_file = current_file
        episodes = result.get('result', {}).get('episodes', [])
        for episode in episodes:
            # Find position of current episode
            episode_library_file = episode.get('file')
            if current_episode_id == episode.get('episodeid'):
                found_match = True
                current_library_file = episode_library_file
                continue
            # Check if it may be a multi-part episode
            if episode_library_file in (current_file, current_library_file):
                continue
            # Skip already watched episodes?
            if not include_watched and episode.get('playcount') > 0:
                continue
            if found_match:
                return episode

        # No next episode found
        _log('No next episode found', 1)
        return None
