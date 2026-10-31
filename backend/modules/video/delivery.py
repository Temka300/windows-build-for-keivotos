"""The protected local XML parser travels with the optional Video owner."""
from delivery_contract import DeliveryContribution


def contribution(target):
    return DeliveryContribution(collect_packages=('defusedxml',),metadata_packages=('defusedxml',))
