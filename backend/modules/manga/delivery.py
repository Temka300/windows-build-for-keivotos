"""Manga retains its protected local XML parser independently of Video."""
from delivery_contract import DeliveryContribution


def contribution(target):
    return DeliveryContribution(collect_packages=('defusedxml',),metadata_packages=('defusedxml',))
