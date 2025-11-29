"""
Dispatcharr Timeshift Plugin - Hooks

Implements timeshift via monkey-patching (no modification to Dispatcharr source):
1. Patches xc_get_live_streams to add tv_archive and use provider's stream_id
2. Patches stream_xc to find channels by provider stream_id (for live streaming)
3. Patches xc_get_epg to find channels by provider stream_id (for EPG/timeshift data)
4. Patches URLResolver.resolve to intercept /timeshift/ URLs
5. Patches generate_epg to convert XMLTV timestamps to local timezone (fixes IPTVX offset)

RUNTIME ENABLE/DISABLE:
    Hooks are installed once at startup (regardless of plugin enabled state).
    Each hook checks _is_plugin_enabled() at runtime before executing its logic.
    This allows enabling/disabling the plugin without restarting Dispatcharr.

    Why this approach?
    - Dispatcharr's PluginManager only toggles the 'enabled' flag in database
    - It does NOT call plugin.run("enable") or plugin.run("disable")
    - So we can't rely on those callbacks to install/uninstall hooks dynamically
    - Instead, hooks are always installed but check enabled state per-request

WHY MONKEY-PATCHING?
    We tried several approaches before settling on this:

    1. URL pattern injection (urlpatterns.insert) - FAILED
       Dispatcharr has a catch-all pattern "<path:unused_path>" that matches
       everything. Even inserting before it didn't work reliably.

    2. Middleware - FAILED
       Middleware runs after URL resolution, so the catch-all already matched.

    3. ROOT_URLCONF replacement - FAILED
       Django caches settings at startup, so changing ROOT_URLCONF had no effect.

    4. URLResolver.resolve patching - WORKS!
       By patching the resolve() method on the URLResolver class, we intercept
       URL resolution BEFORE any patterns are checked.

CRITICAL: stream_id in API response
    iPlayTV uses the stream_id from get_live_streams API for BOTH:
    - Live streaming: /live/user/pass/{stream_id}.ts
    - Timeshift: /timeshift/user/pass/.../stream_id.ts

    We MUST change stream_id to provider's ID so timeshift works.
    But this breaks live streaming because Dispatcharr's stream_xc looks up by internal ID.

    Solution: Also patch stream_xc to first try provider stream_id lookup.

GitHub: https://github.com/cedric-marcoux/dispatcharr_timeshift
"""

import re
import logging

logger = logging.getLogger("plugins.dispatcharr_timeshift.hooks")

# Store original functions for potential restoration
_original_xc_get_live_streams = None
_original_stream_xc = None
_original_xc_get_epg = None
_original_generate_epg = None
_original_url_callbacks = {}
_original_resolve = None


def _is_plugin_enabled():
    """
    Check if plugin is enabled in database.

    Called at runtime by each patched function to determine if timeshift
    logic should execute. This enables hot enable/disable without restart.

    Returns:
        bool: True if plugin is enabled, False otherwise
    """
    try:
        from apps.plugins.models import PluginConfig
        config = PluginConfig.objects.get(key='dispatcharr_timeshift')
        return config.enabled
    except Exception:
        return False


def install_hooks():
    """
    Install all timeshift hooks.

    Returns:
        bool: True if successful, False otherwise
    """
    logger.info("[Timeshift] Installing hooks...")

    try:
        _patch_xc_get_live_streams()
        _patch_stream_xc()
        _patch_xc_get_epg()
        _patch_generate_epg()
        _patch_url_resolver()
        logger.info("[Timeshift] All hooks installed successfully")
        return True
    except Exception as e:
        logger.error(f"[Timeshift] Failed to install hooks: {e}", exc_info=True)
        return False


def uninstall_hooks():
    """
    Restore all original functions.
    """
    logger.info("[Timeshift] Uninstalling hooks...")
    _restore_xc_get_live_streams()
    _restore_stream_xc()
    _restore_xc_get_epg()
    _restore_generate_epg()
    _restore_url_resolver()
    logger.info("[Timeshift] All hooks uninstalled")


def _patch_xc_get_live_streams():
    """
    Patch xc_get_live_streams to:
    1. Add tv_archive and tv_archive_duration from provider
    2. Replace stream_id with provider's stream_id

    WHY REPLACE stream_id?
        iPlayTV uses stream_id for timeshift URLs. If we keep Dispatcharr's
        internal ID, iPlayTV sends that ID in timeshift requests, and we
        can't find the channel because we search by provider stream_id.

        We also patch stream_xc to handle live streaming with provider IDs.
    """
    global _original_xc_get_live_streams

    from apps.output import views as output_views

    _original_xc_get_live_streams = output_views.xc_get_live_streams

    def patched_xc_get_live_streams(request, user, category_id=None):
        streams = _original_xc_get_live_streams(request, user, category_id)

        # Skip if plugin is disabled
        if not _is_plugin_enabled():
            return streams

        from apps.channels.models import Channel

        for stream_data in streams:
            try:
                channel = Channel.objects.filter(id=stream_data.get('stream_id')).first()
                if not channel:
                    continue

                first_stream = channel.streams.order_by('channelstream__order').first()
                if not first_stream:
                    continue

                props = first_stream.custom_properties or {}

                # Add tv_archive values
                stream_data['tv_archive'] = int(props.get('tv_archive', 0))
                stream_data['tv_archive_duration'] = int(props.get('tv_archive_duration', 0))

                # Replace stream_id with provider's stream_id
                # This is needed for iPlayTV to construct correct timeshift URLs
                provider_stream_id = props.get('stream_id')
                if provider_stream_id:
                    stream_data['stream_id'] = int(provider_stream_id)

            except Exception as e:
                logger.debug(f"[Timeshift] Error enhancing stream: {e}")

        return streams

    output_views.xc_get_live_streams = patched_xc_get_live_streams
    logger.info("[Timeshift] Patched xc_get_live_streams")


def _restore_xc_get_live_streams():
    """Restore original xc_get_live_streams function."""
    global _original_xc_get_live_streams

    if _original_xc_get_live_streams:
        from apps.output import views as output_views
        output_views.xc_get_live_streams = _original_xc_get_live_streams
        _original_xc_get_live_streams = None
        logger.info("[Timeshift] Restored xc_get_live_streams")


def _patch_stream_xc():
    """
    Patch stream_xc to find channels by provider stream_id first.

    WHY THIS PATCH?
        After patching xc_get_live_streams to return provider's stream_id,
        iPlayTV uses that ID in live stream URLs: /live/user/pass/{provider_id}.ts

        But Dispatcharr's stream_xc looks up Channel.objects.get(id=channel_id),
        which fails because the provider ID doesn't match internal IDs.

        This patch first tries to find channel by provider stream_id in
        custom_properties, then falls back to internal ID lookup.

    WHY PATCH URL PATTERNS?
        Simply patching the function in the module doesn't work because Django
        URL patterns keep a reference to the original function from import time.
        We must also update the callback in the urlpatterns list.
    """
    global _original_stream_xc, _original_url_callbacks

    from apps.proxy.ts_proxy import views as proxy_views
    from dispatcharr import urls as main_urls

    _original_stream_xc = proxy_views.stream_xc

    def patched_stream_xc(request, username, password, channel_id):
        # If plugin is disabled, use original function
        if not _is_plugin_enabled():
            return _original_stream_xc(request, username, password, channel_id)

        import pathlib
        from django.shortcuts import get_object_or_404
        from rest_framework.response import Response
        from apps.accounts.models import User
        from apps.channels.models import Channel, Stream

        user = get_object_or_404(User, username=username)

        # Extract channel ID without extension (e.g., "12345.ts" -> "12345")
        channel_id_str = pathlib.Path(channel_id).stem

        custom_properties = user.custom_properties or {}

        if "xc_password" not in custom_properties:
            return Response({"error": "Invalid credentials"}, status=401)

        if custom_properties["xc_password"] != password:
            return Response({"error": "Invalid credentials"}, status=401)

        channel = None

        # TIMESHIFT FIX: First try to find by provider stream_id
        # This handles the case where API returns provider's stream_id
        stream = Stream.objects.filter(
            custom_properties__stream_id=channel_id_str,
            m3u_account__account_type='XC'
        ).first()
        if stream:
            channel = stream.channels.first()
            if channel:
                logger.info(f"[Timeshift] Live: Found channel by provider stream_id={channel_id_str}: {channel.name}")

        # Fall back to original behavior (internal ID lookup)
        if not channel:
            try:
                internal_id = int(channel_id_str)
                if user.user_level < 10:
                    user_profile_count = user.channel_profiles.count()

                    if user_profile_count == 0:
                        filters = {
                            "id": internal_id,
                            "user_level__lte": user.user_level
                        }
                        channel = Channel.objects.filter(**filters).first()
                    else:
                        filters = {
                            "id": internal_id,
                            "channelprofilemembership__enabled": True,
                            "user_level__lte": user.user_level,
                            "channelprofilemembership__channel_profile__in": user.channel_profiles.all()
                        }
                        channel = Channel.objects.filter(**filters).distinct().first()
                else:
                    channel = Channel.objects.filter(id=internal_id).first()
            except (ValueError, TypeError):
                pass

        if not channel:
            logger.warning(f"[Timeshift] Live: Channel not found for ID: {channel_id_str}")
            return Response({"error": "Not found"}, status=404)

        # Check user access level
        if user.user_level < channel.user_level:
            return Response({"error": "Not found"}, status=404)

        # Call the original stream_ts function
        from apps.proxy.ts_proxy.views import stream_ts
        # Handle both DRF requests and regular Django requests
        actual_request = getattr(request, '_request', request)
        return stream_ts(actual_request, str(channel.uuid))

    # Patch the module (for any new imports)
    proxy_views.stream_xc = patched_stream_xc

    # CRITICAL: Also patch the URL patterns callbacks
    # Django keeps references to the original function in urlpatterns
    # Store original callbacks so we can restore them later
    for pattern in main_urls.urlpatterns:
        if hasattr(pattern, 'callback') and pattern.callback == _original_stream_xc:
            _original_url_callbacks[id(pattern)] = _original_stream_xc
            pattern.callback = patched_stream_xc
            logger.info(f"[Timeshift] Patched URL pattern: {pattern.name}")

    logger.info("[Timeshift] Patched stream_xc for provider stream_id lookup")


def _restore_stream_xc():
    """Restore original stream_xc function and URL pattern callbacks."""
    global _original_stream_xc, _original_url_callbacks

    if _original_stream_xc:
        from apps.proxy.ts_proxy import views as proxy_views
        from dispatcharr import urls as main_urls

        # Restore module function
        proxy_views.stream_xc = _original_stream_xc

        # Restore URL pattern callbacks
        for pattern in main_urls.urlpatterns:
            if id(pattern) in _original_url_callbacks:
                pattern.callback = _original_url_callbacks[id(pattern)]
                logger.info(f"[Timeshift] Restored URL pattern: {pattern.name}")

        _original_url_callbacks = {}
        _original_stream_xc = None
        logger.info("[Timeshift] Restored stream_xc")


def _patch_xc_get_epg():
    """
    Patch xc_get_epg to find channels by provider stream_id first.

    WHY THIS PATCH?
        After patching xc_get_live_streams to return provider's stream_id,
        IPTV clients use that ID when requesting EPG data via player_api.php
        with action=get_simple_data_table or get_short_epg.

        But Dispatcharr's xc_get_epg looks up Channel.objects.filter(id=stream_id),
        which fails because the provider ID doesn't match internal IDs.

        This patch first tries to find channel by provider stream_id in
        custom_properties, then falls back to internal ID lookup.
    """
    global _original_xc_get_epg

    from apps.output import views as output_views

    _original_xc_get_epg = output_views.xc_get_epg

    def patched_xc_get_epg(request, user, short=False):
        # If plugin is disabled, use original function
        if not _is_plugin_enabled():
            return _original_xc_get_epg(request, user, short)

        from django.http import Http404
        from apps.channels.models import Channel, Stream

        channel_id = request.GET.get('stream_id')
        if not channel_id:
            raise Http404()

        channel = None

        # TIMESHIFT FIX: First try to find by provider stream_id
        # This handles the case where API returns provider's stream_id
        stream = Stream.objects.filter(
            custom_properties__stream_id=str(channel_id),
            m3u_account__account_type='XC'
        ).first()
        if stream:
            channel = stream.channels.first()
            if channel:
                logger.info(f"[Timeshift] EPG: Found channel by provider stream_id={channel_id}: {channel.name}")

        # Fall back to original behavior (internal ID lookup)
        if not channel:
            if user.user_level < 10:
                user_profile_count = user.channel_profiles.count()

                if user_profile_count == 0:
                    channel = Channel.objects.filter(
                        id=channel_id,
                        user_level__lte=user.user_level
                    ).first()
                else:
                    filters = {
                        "id": channel_id,
                        "channelprofilemembership__enabled": True,
                        "user_level__lte": user.user_level,
                        "channelprofilemembership__channel_profile__in": user.channel_profiles.all()
                    }
                    channel = Channel.objects.filter(**filters).distinct().first()
            else:
                channel = Channel.objects.filter(id=channel_id).first()

        if not channel:
            logger.warning(f"[Timeshift] EPG: Channel not found for ID: {channel_id}")
            raise Http404()

        # Now call the original function's logic with the found channel
        # We need to temporarily modify request.GET to use the internal channel ID
        from django.http import QueryDict
        original_get = request.GET

        # Create a mutable copy and update stream_id to internal ID
        new_get = original_get.copy()
        new_get['stream_id'] = str(channel.id)
        request.GET = new_get

        # Check if channel has tv_archive enabled
        first_stream = channel.streams.order_by('channelstream__order').first()
        props = first_stream.custom_properties or {} if first_stream else {}
        has_tv_archive = props.get('tv_archive') in (1, '1')

        if has_tv_archive and not short:
            # CUSTOM EPG QUERY: Include past programs for timeshift
            # Instead of calling original function, we build EPG ourselves
            from datetime import datetime, timezone, timedelta
            from django.utils import timezone as django_timezone
            import base64

            archive_duration_days = int(props.get('tv_archive_duration', 7))
            start_date = django_timezone.now() - timedelta(days=archive_duration_days)

            # Get programs from the last X days until now + 7 days in future
            programs = channel.epg_data.programs.filter(
                start_time__gte=start_date
            ).order_by('start_time') if channel.epg_data else []

            output = {"epg_listings": []}
            now = django_timezone.now()

            # Get timezone from plugin settings or default to Europe/Brussels
            from zoneinfo import ZoneInfo
            try:
                from apps.plugins.models import PluginConfig
                config = PluginConfig.objects.filter(key='dispatcharr_timeshift').first()
                timezone_str = config.config.get('timezone', 'Europe/Brussels') if config and config.config else 'Europe/Brussels'
            except Exception:
                timezone_str = 'Europe/Brussels'

            local_tz = ZoneInfo(timezone_str)

            for program in programs:
                start = program.start_time
                end = program.end_time
                title = program.title
                description = program.description

                # Convert timestamps to local timezone (Europe/Brussels)
                # Original provider sends 'start' field in local time, not UTC
                # Snappier expects this format to match the user's timezone
                local_tz = ZoneInfo("Europe/Brussels")
                start_local = start.astimezone(local_tz)
                end_local = end.astimezone(local_tz)

                # Generate unique ID for each program using timestamp
                # This is critical for clients like Snappier to distinguish programs
                program_id = int(start.timestamp())

                program_output = {
                    "id": str(program_id),
                    "epg_id": str(program.id) if hasattr(program, 'id') and program.id else str(program_id),
                    "title": base64.b64encode(title.encode()).decode(),
                    "lang": "fr",  # Match provider's language field
                    "start": start_local.strftime("%Y-%m-%d %H:%M:%S"),    # Local time (Europe/Brussels) - match original provider
                    "end": end_local.strftime("%Y-%m-%d %H:%M:%S"),        # Local time (Europe/Brussels) - match original provider
                    "description": base64.b64encode(description.encode()).decode(),
                    "channel_id": props.get('epg_channel_id') or str(channel.id),  # Use EPG channel ID from provider (e.g., "RTSUn.ch")
                    "start_timestamp": str(int(start.timestamp())),  # STRING not int - match provider format
                    "stop_timestamp": str(int(end.timestamp())),     # STRING not int - match provider format
                    "stream_id": props.get('stream_id'),  # Use provider's stream_id, not internal channel ID
                    "now_playing": 0 if start > now or end < now else 1,
                }

                # Set has_archive for past programs within archive duration
                if end < now:
                    days_ago = (now - end).days
                    if days_ago <= archive_duration_days:
                        program_output["has_archive"] = 1  # INTEGER not string - match provider format
                        logger.debug(f"[Timeshift] EPG: Set has_archive=1 for '{title}' ending {days_ago} days ago")
                    else:
                        program_output["has_archive"] = 0  # INTEGER not string
                else:
                    program_output["has_archive"] = 0  # INTEGER not string

                output['epg_listings'].append(program_output)

            logger.info(f"[Timeshift] EPG: Generated {len(output['epg_listings'])} programs for channel {channel.name} (including past {archive_duration_days} days)")
            # Restore original GET params
            request.GET = original_get
            return output
        else:
            # No timeshift or short=True, use original function
            try:
                result = _original_xc_get_epg(request, user, short)
                return result
            finally:
                # Restore original GET params
                request.GET = original_get

    output_views.xc_get_epg = patched_xc_get_epg
    logger.info("[Timeshift] Patched xc_get_epg for provider stream_id lookup")


def _restore_xc_get_epg():
    """Restore original xc_get_epg function."""
    global _original_xc_get_epg

    if _original_xc_get_epg:
        from apps.output import views as output_views
        output_views.xc_get_epg = _original_xc_get_epg
        _original_xc_get_epg = None
        logger.info("[Timeshift] Restored xc_get_epg")


def _patch_generate_epg():
    """
    Patch generate_epg to convert XMLTV timestamps to local timezone.

    WHY THIS PATCH?
        IPTVX and other IPTV clients fetch EPG data via /output/epg endpoint
        which returns XMLTV format. The timestamps in XMLTV are formatted as:

            start="20251128143000 +0000"

        Django stores all datetime fields in UTC, so when Dispatcharr formats
        timestamps with strftime("%Y%m%d%H%M%S %z"), they're output in UTC.

        However, IPTVX displays these timestamps as-is without timezone conversion,
        causing a 1-hour offset for users in Europe/Brussels (UTC+1).

        This patch wraps the generate_epg generator to intercept timestamp
        formatting and convert to local timezone before output.
    """
    global _original_generate_epg

    from apps.output import views as output_views

    _original_generate_epg = output_views.generate_epg

    def patched_generate_epg(request, profile_name=None, user=None):
        # If plugin is disabled, use original function
        if not _is_plugin_enabled():
            return _original_generate_epg(request, profile_name, user)

        # Get timezone from plugin settings
        from zoneinfo import ZoneInfo
        try:
            from apps.plugins.models import PluginConfig
            config = PluginConfig.objects.filter(key='dispatcharr_timeshift').first()
            timezone_str = config.config.get('timezone', 'Europe/Brussels') if config and config.config else 'Europe/Brussels'
        except Exception:
            timezone_str = 'Europe/Brussels'

        local_tz = ZoneInfo(timezone_str)
        logger.info(f"[Timeshift] XMLTV: Converting timestamps to {timezone_str}")

        # Call original function to get StreamingHttpResponse
        original_response = _original_generate_epg(request, profile_name, user)

        # Extract the original generator from StreamingHttpResponse
        original_generator = original_response.streaming_content

        # Wrap the generator to intercept and modify timestamp lines
        import re
        timestamp_pattern = re.compile(r'(\d{14}) ([+-]\d{4})')

        def timezone_converting_generator():
            for chunk in original_generator:
                # Ensure chunk is string (might be bytes)
                if isinstance(chunk, bytes):
                    chunk = chunk.decode('utf-8')

                # Only process chunks that contain programme tags with timestamps
                if 'programme start=' in chunk or 'start="' in chunk or 'stop="' in chunk:
                    # Replace timestamp format: convert from UTC to local timezone
                    # Pattern matches: 20251128143000 +0000
                    def convert_timestamp(match):
                        from datetime import datetime
                        timestamp_str = match.group(1)
                        original_tz = match.group(2)

                        # Parse the UTC timestamp
                        try:
                            utc_time = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
                            utc_time = utc_time.replace(tzinfo=ZoneInfo("UTC"))

                            # Convert to local timezone
                            local_time = utc_time.astimezone(local_tz)

                            # Format back to XMLTV format
                            return local_time.strftime("%Y%m%d%H%M%S %z")
                        except Exception as e:
                            logger.warning(f"[Timeshift] XMLTV timestamp conversion failed: {e}")
                            return match.group(0)  # Return original if conversion fails

                    chunk = timestamp_pattern.sub(convert_timestamp, chunk)

                yield chunk

        # Return a new StreamingHttpResponse with our wrapped generator
        from django.http import StreamingHttpResponse
        response = StreamingHttpResponse(
            timezone_converting_generator(),
            content_type='application/xml'
        )
        response['Content-Disposition'] = 'attachment; filename="Dispatcharr.xml"'
        response['Cache-Control'] = 'no-cache'
        return response

    output_views.generate_epg = patched_generate_epg
    logger.info("[Timeshift] Patched generate_epg for XMLTV timezone conversion")


def _restore_generate_epg():
    """Restore original generate_epg function."""
    global _original_generate_epg

    if _original_generate_epg:
        from apps.output import views as output_views
        output_views.generate_epg = _original_generate_epg
        _original_generate_epg = None
        logger.info("[Timeshift] Restored generate_epg")


def _patch_url_resolver():
    """
    Patch URLResolver.resolve to intercept /timeshift/ URLs.

    WHY THIS APPROACH:
        Dispatcharr's urls.py has a catch-all pattern at the end:
            path("<path:unused_path>", views.handle_404)

        This catches ALL unmatched URLs, including our /timeshift/ URLs.
        By patching URLResolver.resolve(), we intercept the URL BEFORE
        any pattern matching happens.

    URL FORMAT FROM iPlayTV:
        /timeshift/{user}/{pass}/{epg_channel}/{timestamp}/{provider_stream_id}.ts

        QUIRK: iPlayTV sends parameters in unexpected positions:
        - Position 3 (stream_id param) = EPG channel number (NOT used)
        - Position 5 (duration param) = Provider's stream_id (USED for lookup)
    """
    global _original_resolve

    from django.urls.resolvers import URLResolver

    # Already patched if _original_resolve is set
    if _original_resolve is not None:
        logger.info("[Timeshift] URLResolver already patched")
        return

    from .views import timeshift_proxy

    TIMESHIFT_PATTERN = re.compile(
        r'^/?timeshift/(?P<username>[^/]+)/(?P<password>[^/]+)/'
        r'(?P<stream_id>\d+)/(?P<timestamp>[\d\-:]+)/(?P<duration>\d+)\.ts$'
    )

    _original_resolve = URLResolver.resolve

    def patched_resolve(self, path):
        # Only intercept if plugin is enabled
        if _is_plugin_enabled() and (path.startswith('/timeshift/') or path.startswith('timeshift/')):
            match = TIMESHIFT_PATTERN.match(path)
            if match:
                from django.urls import ResolverMatch
                logger.debug(f"[Timeshift] Intercepted: {path}")
                return ResolverMatch(
                    timeshift_proxy,
                    (),
                    match.groupdict(),
                    route=path,
                )
        return _original_resolve(self, path)

    URLResolver.resolve = patched_resolve
    logger.info("[Timeshift] Patched URLResolver.resolve")


def _restore_url_resolver():
    """Restore original URLResolver.resolve function."""
    global _original_resolve

    if _original_resolve is not None:
        from django.urls.resolvers import URLResolver
        URLResolver.resolve = _original_resolve
        _original_resolve = None
        logger.info("[Timeshift] Restored URLResolver.resolve")
