# -*- coding: utf8 -*-
import pytest
import gevent
import gevent.monkey
from ethereum.keys import privtoaddr, PBKDF2_CONSTANTS
from ethereum._solidity import compile_file
from pyethapp.rpc_client import JSONRPCClient

from raiden.utils import sha3
from raiden.tests.utils.tests import cleanup_tasks
from raiden.network.transport import UDPTransport
from raiden.network.rpc.client import (
    get_contract_path,
    BlockChainService,
    BlockChainServiceMock,
    MOCK_REGISTRY_ADDRESS,
)
from raiden.tests.utils.network import (
    create_network,
    create_sequential_network,
    create_hydrachain_network,
)

# we need to use fixture for the default values otherwise
# pytest.mark.parametrize won't work (pytest 2.9.2)

# pylint: disable=redefined-outer-name,too-many-arguments,unused-argument,too-many-locals

# otherwise running hydrachain will block the test
gevent.monkey.patch_socket()
gevent.get_hub().SYSTEM_ERROR = BaseException
PBKDF2_CONSTANTS['c'] = 100


@pytest.fixture
def hydrachainkey_seed():
    return 'hydrachain:{}'


@pytest.fixture
def hydrachain_number_of_nodes():
    return 3


@pytest.fixture
def hydrachain_p2p_base_port():
    # TODO: return a base port that is not random and guaranteed to be used
    # only once (avoid that a badly cleaned test interfere with the next).
    return 29870


@pytest.fixture
def number_of_nodes():
    """ Number of raiden nodes. """
    return 3


@pytest.fixture
def privatekey_seed():
    """ Raiden private key template. """
    return 'key:{}'


@pytest.fixture
def private_keys(number_of_nodes, privatekey_seed):
    return [
        sha3(privatekey_seed.format(position))
        for position in range(number_of_nodes)
    ]


@pytest.fixture
def hydrachain_private_keys(hydrachain_number_of_nodes, hydrachainkey_seed):
    return [
        sha3(hydrachainkey_seed.format(position))
        for position in range(hydrachain_number_of_nodes)
    ]


@pytest.fixture
def hydrachain_network(request, private_keys, hydrachain_private_keys,
                       hydrachain_p2p_base_port, tmpdir):
    hydrachain_apps = create_hydrachain_network(
        private_keys,
        hydrachain_private_keys,
        hydrachain_p2p_base_port,
        str(tmpdir),
    )

    def _cleanup():
        # First allow the services to cleanup themselves
        for app in hydrachain_apps:
            app.stop()

        # Then kill any remaining tasklet
        cleanup_tasks()

    request.addfinalizer(_cleanup)

    return hydrachain_apps


@pytest.fixture
def asset():
    """ Raiden chain asset. """
    return sha3('asset')[:20]


@pytest.fixture
def deposit():
    """ Raiden chain default deposit. """
    return 100


@pytest.fixture
def registry_address():
    return MOCK_REGISTRY_ADDRESS


@pytest.fixture
def number_of_assets():
    return 1


@pytest.fixture
def assets_addresses(number_of_assets):
    return [
        sha3('asset:{}'.format(number))[:20]
        for number in range(number_of_assets)
    ]


@pytest.fixture
def channels_per_node():
    """ Number of channels per node in for the raiden_network fixture. """
    return 1


@pytest.fixture
def transport_class():
    return UDPTransport


@pytest.fixture
def blockchain_service(request, registry_address):
    """ A fixture to clean up the singleton. """
    # pylint: disable=protected-access
    def _cleanup():
        BlockChainServiceMock._instance = None

    request.addfinalizer(_cleanup)

    # allows the fixture to instantiate the blockchain
    BlockChainServiceMock._instance = True

    blockchain_service = BlockChainServiceMock(None, registry_address)

    # overwrite the instance
    BlockChainServiceMock._instance = blockchain_service  # pylint: disable=redefined-variable-type

    return blockchain_service


@pytest.fixture
def raiden_chain(request, private_keys, asset, deposit, registry_address,
                 blockchain_service, transport_class):
    blockchain_service_class = BlockChainServiceMock
    blockchain_service.new_channel_manager_contract(asset)

    raiden_apps = create_sequential_network(
        private_keys,
        deposit,
        asset,
        registry_address,
        transport_class,
        blockchain_service_class,
    )

    def _cleanup():
        # First allow the services to cleanup themselves
        for app in raiden_apps:
            app.stop()

        # Then kill any remaining tasklet
        cleanup_tasks()

    request.addfinalizer(_cleanup)

    return raiden_apps


@pytest.fixture
def raiden_network(request, private_keys, assets_addresses, channels_per_node,
                   deposit, registry_address, blockchain_service,
                   transport_class):
    blockchain_service_class = BlockChainServiceMock

    for asset in assets_addresses:
        blockchain_service.new_channel_manager_contract(asset)

    raiden_apps = create_network(
        private_keys,
        assets_addresses,
        registry_address,
        channels_per_node,
        deposit,
        transport_class,
        blockchain_service_class,
    )

    def _cleanup():
        # First allow the services to cleanup themselves
        for app in raiden_apps:
            app.stop()

        # Then kill any remaining tasklet
        cleanup_tasks()
    request.addfinalizer(_cleanup)

    return raiden_apps


@pytest.fixture
def deployed_network(request, private_keys, hydrachain_network,
                     channels_per_node, deposit, number_of_assets, timeout,
                     transport_class):
    privatekey = private_keys[0]
    address = privtoaddr(privatekey)
    blockchain_service_class = BlockChainService

    jsonrpc_client = JSONRPCClient(
        privkey=privatekey,
        print_communication=False,
    )

    humantoken_path = get_contract_path('HumanStandardToken.sol')
    registry_path = get_contract_path('Registry.sol')

    humantoken_contracts = compile_file(humantoken_path, libraries=dict())
    registry_contracts = compile_file(registry_path, libraries=dict())

    registry_proxy = jsonrpc_client.deploy_solidity_contract(
        address,
        'Registry',
        registry_contracts,
        dict(),
        tuple(),
        timeout=timeout,
    )
    registry_address = registry_proxy.address

    total_asset = 2 * deposit * len(private_keys)
    asset_addresses = []
    for _ in range(number_of_assets):
        token_proxy = jsonrpc_client.deploy_solidity_contract(
            address,
            'HumanStandardToken',
            humantoken_contracts,
            dict(),
            (total_asset, 'raiden', 2, 'Rd'),
            timeout=timeout,
        )
        asset_address = token_proxy.address
        asset_addresses.append(asset_address)

        transaction_hash = registry_proxy.addAsset(asset_address)  # pylint: disable=no-member
        jsonrpc_client.poll(transaction_hash.decode('hex'), timeout=timeout)

        # only the creator of the token starts with a balance, transfer from
        # the creator to the other nodes
        for transfer_to in private_keys:
            if transfer_to != jsonrpc_client.privkey:
                token_proxy.transfer(privtoaddr(transfer_to), 2 * deposit)  # pylint: disable=no-member

    raiden_apps = create_network(
        private_keys,
        asset_addresses,
        registry_address,
        channels_per_node,
        deposit,
        transport_class,
        blockchain_service_class,
    )

    def _cleanup():
        # First allow the services to cleanup themselves
        for app in raiden_apps:
            app.stop()

        # Then kill any remaining tasklet
        cleanup_tasks()
    request.addfinalizer(_cleanup)

    return raiden_apps
