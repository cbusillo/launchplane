from ipaddress import IPv4Address, IPv6Address


def is_public_address(address: IPv4Address | IPv6Address) -> bool:
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        return False
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None and not is_public_address(address.ipv4_mapped):
            return False
        if address.sixtofour is not None and not is_public_address(address.sixtofour):
            return False
        if address.teredo is not None and any(
            not is_public_address(embedded_address) for embedded_address in address.teredo
        ):
            return False
    return True
