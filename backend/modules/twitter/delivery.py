"""Declare existing downloader and image/FFmpeg dependencies for removable Twitter."""
from delivery_contract import DeliveryContribution


def contribution(target):
    return DeliveryContribution(hidden_imports=('modules.twitter.worker', 'yt_dlp.networking._urllib'),
                                collect_packages=('yt_dlp',), metadata_packages=('yt-dlp',))
