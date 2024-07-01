import asyncio
import unittest
from decimal import Decimal
from typing import Awaitable
from unittest.mock import AsyncMock, MagicMock, patch

from xrpl.models import Request, Response, Transaction
from xrpl.models.requests.request import RequestMethod
from xrpl.models.response import ResponseStatus, ResponseType
from xrpl.models.transactions.types import TransactionType

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.exchange.xrpl import xrpl_constants as CONSTANTS
from hummingbot.connector.exchange.xrpl.xrpl_api_order_book_data_source import XRPLAPIOrderBookDataSource
from hummingbot.connector.exchange.xrpl.xrpl_api_user_stream_data_source import XRPLAPIUserStreamDataSource
from hummingbot.connector.exchange.xrpl.xrpl_auth import XRPLAuth
from hummingbot.connector.exchange.xrpl.xrpl_exchange import XrplExchange
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_tracker import OrderBookTracker
from hummingbot.core.data_type.user_stream_tracker import UserStreamTracker


class XRPLAPIOrderBookDataSourceUnitTests(unittest.TestCase):
    # logging.Level required to receive logs from the data source logger
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.ev_loop = asyncio.get_event_loop()
        cls.base_asset = "SOLO"
        cls.quote_asset = "XRP"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.trading_pair_usd = f"{cls.base_asset}-USD"

    def setUp(self) -> None:
        super().setUp()
        self.log_records = []
        self.listening_task = None

        client_config_map = ClientConfigAdapter(ClientConfigMap())
        self.connector = XrplExchange(
            client_config_map=client_config_map,
            xrpl_secret_key="",
            wss_node_url="wss://sample.com",
            wss_second_node_url="wss://sample.com",
            trading_pairs=[self.trading_pair],
            trading_required=False,
        )
        self.data_source = XRPLAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.connector._web_assistants_factory,
        )
        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)
        self.data_source._request_order_book_snapshot = AsyncMock()
        self.data_source._request_order_book_snapshot.return_value = self._snapshot_response()

        self._original_full_order_book_reset_time = self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS
        self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS = -1
        self.resume_test_event = asyncio.Event()

        exchange_market_info = CONSTANTS.MARKETS
        self.connector._initialize_trading_pair_symbols_from_exchange_info(exchange_market_info)

        trading_rule = TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=Decimal("1e-6"),
            min_price_increment=Decimal("1e-6"),
            min_quote_amount_increment=Decimal("1e-6"),
            min_base_amount_increment=Decimal("1e-15"),
            min_notional_size=Decimal("1e-6"),
        )

        trading_rule_usd = TradingRule(
            trading_pair=self.trading_pair_usd,
            min_order_size=Decimal("1e-6"),
            min_price_increment=Decimal("1e-6"),
            min_quote_amount_increment=Decimal("1e-6"),
            min_base_amount_increment=Decimal("1e-6"),
            min_notional_size=Decimal("1e-6"),
        )

        self.connector._trading_rules[self.trading_pair] = trading_rule
        self.connector._trading_rules[self.trading_pair_usd] = trading_rule_usd

        trading_rules_info = {
            self.trading_pair: {"base_transfer_rate": 0.01, "quote_transfer_rate": 0.01},
            self.trading_pair_usd: {"base_transfer_rate": 0.01, "quote_transfer_rate": 0.01},
        }
        trading_pair_fee_rules = self.connector._format_trading_pair_fee_rules(trading_rules_info)

        for trading_pair_fee_rule in trading_pair_fee_rules:
            self.connector._trading_pair_fee_rules[trading_pair_fee_rule["trading_pair"]] = trading_pair_fee_rule

        self.data_source._xrpl_client = AsyncMock()
        self.data_source._xrpl_client.__aenter__.return_value = self.data_source._xrpl_client
        self.data_source._xrpl_client.__aexit__.return_value = None

        self.connector._orderbook_ds = self.data_source
        self.connector._set_order_book_tracker(
            OrderBookTracker(
                data_source=self.connector._orderbook_ds,
                trading_pairs=self.connector.trading_pairs,
                domain=self.connector.domain,
            )
        )

        self.connector.order_book_tracker.start()

        self.user_stream_source = XRPLAPIUserStreamDataSource(
            auth=XRPLAuth(xrpl_secret_key=""),
            connector=self.connector,
        )
        self.user_stream_source.logger().setLevel(1)
        self.user_stream_source.logger().addHandler(self)
        self.user_stream_source._xrpl_client = AsyncMock()
        self.user_stream_source._xrpl_client.__aenter__.return_value = self.data_source._xrpl_client
        self.user_stream_source._xrpl_client.__aexit__.return_value = None

        self.connector._user_stream_tracker = UserStreamTracker(data_source=self.user_stream_source)

        self.connector._xrpl_client = AsyncMock()
        self.connector._xrpl_client.__aenter__.return_value = self.connector._xrpl_client
        self.connector._xrpl_client.__aexit__.return_value = None

        self.connector._xrpl_place_order_client = AsyncMock()
        self.connector._xrpl_place_order_client.__aenter__.return_value = self.connector._xrpl_place_order_client
        self.connector._xrpl_place_order_client.__aexit__.return_value = None

    def tearDown(self) -> None:
        self.listening_task and self.listening_task.cancel()
        self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS = self._original_full_order_book_reset_time
        super().tearDown()

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message for record in self.log_records)

    def _create_exception_and_unlock_test_with_event(self, exception):
        self.resume_test_event.set()
        raise exception

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 5):
        ret = self.ev_loop.run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def _trade_update_event(self):
        trade_data = {
            "trade_type": float(TradeType.SELL.value),
            "trade_id": "example_trade_id",
            "update_id": 123456789,
            "price": Decimal("0.001"),
            "amount": Decimal("1"),
            "timestamp": 123456789,
        }

        resp = {"trading_pair": self.trading_pair, "trades": trade_data}
        return resp

    def _snapshot_response(self):
        resp = {
            "asks": [
                {
                    "Account": "r9aZRryD8AZzGqQjYrQQuBBzebjF555Xsa",  # noqa: mock
                    "BookDirectory": "5C8970D155D65DB8FF49B291D7EFFA4A09F9E8A68D9974B25A07FA0FAB195976",  # noqa: mock
                    "BookNode": "0",
                    "Flags": 131072,
                    "LedgerEntryType": "Offer",
                    "OwnerNode": "0",
                    "PreviousTxnID": "373EA7376A1F9DC150CCD534AC0EF8544CE889F1850EFF0084B46997DAF4F1DA",  # noqa: mock
                    "PreviousTxnLgrSeq": 88935730,
                    "Sequence": 86514258,
                    "TakerGets": {
                        "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                        "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                        "value": "91.846106",
                    },
                    "TakerPays": "20621931",
                    "index": "1395ACFB20A47DE6845CF5DB63CF2E3F43E335D6107D79E581F3398FF1B6D612",  # noqa: mock
                    "owner_funds": "140943.4119268388",
                    "quality": "224527.003899327",
                },
                {
                    "Account": "rhqTdSsJAaEReRsR27YzddqyGoWTNMhEvC",  # noqa: mock
                    "BookDirectory": "5C8970D155D65DB8FF49B291D7EFFA4A09F9E8A68D9974B25A07FA8ECFD95726",  # noqa: mock
                    "BookNode": "0",
                    "Flags": 0,
                    "LedgerEntryType": "Offer",
                    "OwnerNode": "2",
                    "PreviousTxnID": "2C266D54DDFAED7332E5E6EC68BF08CC37CE2B526FB3CFD8225B667C4C1727E1",  # noqa: mock
                    "PreviousTxnLgrSeq": 88935726,
                    "Sequence": 71762354,
                    "TakerGets": {
                        "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                        "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                        "value": "44.527243023",
                    },
                    "TakerPays": "10000000",
                    "index": "186D33545697D90A5F18C1541F2228A629435FC540D473574B3B75FEA7B4B88B",  # noqa: mock
                    "owner_funds": "88.4155435721498",
                    "quality": "224581.6116401958",
                },
            ],
            "bids": [
                {
                    "Account": "rn3uVsXJL7KRTa7JF3jXXGzEs3A2UEfett",  # noqa: mock
                    "BookDirectory": "C73FAC6C294EBA5B9E22A8237AAE80725E85372510A6CA794F0FE48CEADD8471",  # noqa: mock
                    "BookNode": "0",
                    "Flags": 0,
                    "LedgerEntryType": "Offer",
                    "OwnerNode": "0",
                    "PreviousTxnID": "2030FB97569D955921659B150A2F5F02CC9BBFCA95BAC6B8D55D141B0ABFA945",  # noqa: mock
                    "PreviousTxnLgrSeq": 88935721,
                    "Sequence": 74073461,
                    "TakerGets": "187000000",
                    "TakerPays": {
                        "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                        "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                        "value": "836.5292665312212",
                    },
                    "index": "3F41585F327EA3690AD19F2A302C5DF2904E01D39C9499B303DB7FA85868B69F",  # noqa: mock
                    "owner_funds": "6713077567",
                    "quality": "0.000004473418537600113",
                },
                {
                    "Account": "rsoLoDTcxn9wCEHHBR7enMhzQMThkB2w28",  # noqa: mock
                    "BookDirectory": "C73FAC6C294EBA5B9E22A8237AAE80725E85372510A6CA794F0FE48D021C71F2",  # noqa: mock
                    "BookNode": "0",
                    "Expiration": 772644742,
                    "Flags": 0,
                    "LedgerEntryType": "Offer",
                    "OwnerNode": "0",
                    "PreviousTxnID": "226434A5399E210F82F487E8710AE21FFC19FE86FC38F3634CF328FA115E9574",  # noqa: mock
                    "PreviousTxnLgrSeq": 88935719,
                    "Sequence": 69870875,
                    "TakerGets": "90000000",
                    "TakerPays": {
                        "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                        "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                        "value": "402.6077034840102",
                    },
                    "index": "4D31D069F1E2B0F2016DA0F1BF232411CB1B4642A49538CD6BB989F353D52411",  # noqa: mock
                    "owner_funds": "827169016",
                    "quality": "0.000004473418927600114",
                },
            ],
            "trading_pair": "SOLO-XRP",
        }

        return resp

    # noqa: mock
    def _event_message(self):
        resp = {
            "transaction": {
                "Account": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                "Fee": "10",
                "Flags": 786432,
                "LastLedgerSequence": 88954510,
                "Memos": [
                    {
                        "Memo": {
                            "MemoData": "68626F742D313731393430303738313137303331392D42534F585036316263393330633963366139393139386462343432343461383637313231373562313663"  # noqa: mock
                        }
                    }
                ],
                "Sequence": 84437780,
                "SigningPubKey": "ED23BA20D57103E05BA762F0A04FE50878C11BD36B7BF9ADACC3EDBD9E6D320923",  # noqa: mock
                "TakerGets": "502953",
                "TakerPays": {
                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                    "value": "2.239836701211152",
                },
                "TransactionType": "OfferCreate",
                "TxnSignature": "2E87E743DE37738DCF1EE6C28F299C4FF18BDCB064A07E9068F1E920F8ACA6C62766177E82917ED0995635E636E3BB8B4E2F4DDCB198B0B9185041BEB466FD03",  # noqa: mock
                "hash": "undefined",
                "ctid": "C54D567C00030000",  # noqa: mock
                "meta": "undefined",
                "validated": "undefined",
                "date": 772789130,
                "ledger_index": "undefined",
                "inLedger": "undefined",
                "metaData": "undefined",
                "status": "undefined",
            },
            "meta": {
                "AffectedNodes": [
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Account": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                                "Balance": "56148988",
                                "Flags": 0,
                                "OwnerCount": 3,
                                "Sequence": 84437781,
                            },
                            "LedgerEntryType": "AccountRoot",
                            "LedgerIndex": "2B3020738E7A44FBDE454935A38D77F12DC5A11E0FA6DAE2D9FCF4719FFAA3BC",  # noqa: mock
                            "PreviousFields": {"Balance": "56651951", "Sequence": 84437780},
                            "PreviousTxnID": "BCBB6593A916EDBCC84400948B0525BE7E972B893111FE1C89A7519F8A5ACB2B",  # noqa: mock
                            "PreviousTxnLgrSeq": 88954461,
                        }
                    },
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Account": "rhqTdSsJAaEReRsR27YzddqyGoWTNMhEvC",  # noqa: mock
                                "BookDirectory": "5C8970D155D65DB8FF49B291D7EFFA4A09F9E8A68D9974B25A07F01A195F8476",  # noqa: mock
                                "BookNode": "0",
                                "Flags": 0,
                                "OwnerNode": "2",
                                "Sequence": 71762948,
                                "TakerGets": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                                    "value": "42.50531785780174",
                                },
                                "TakerPays": "9497047",
                            },
                            "LedgerEntryType": "Offer",
                            "LedgerIndex": "3ABFC9B192B73ECE8FB6E2C46E49B57D4FBC4DE8806B79D913C877C44E73549E",  # noqa: mock
                            "PreviousFields": {
                                "TakerGets": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                                    "value": "44.756352009",
                                },
                                "TakerPays": "10000000",
                            },
                            "PreviousTxnID": "7398CE2FDA7FF61B52C1039A219D797E526ACCCFEC4C44A9D920ED28B551B539",  # noqa: mock
                            "PreviousTxnLgrSeq": 88954480,
                        }
                    },
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Account": "rhqTdSsJAaEReRsR27YzddqyGoWTNMhEvC",  # noqa: mock
                                "Balance": "251504663",
                                "Flags": 0,
                                "OwnerCount": 30,
                                "Sequence": 71762949,
                            },
                            "LedgerEntryType": "AccountRoot",
                            "LedgerIndex": "4F7BC1BE763E253402D0CA5E58E7003D326BEA2FEB5C0FEE228660F795466F6E",  # noqa: mock
                            "PreviousFields": {"Balance": "251001710"},
                            "PreviousTxnID": "7398CE2FDA7FF61B52C1039A219D797E526ACCCFEC4C44A9D920ED28B551B539",  # noqa: mock
                            "PreviousTxnLgrSeq": 88954480,
                        }
                    },
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Balance": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                                    "value": "-195.4313653751863",
                                },
                                "Flags": 2228224,
                                "HighLimit": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rhqTdSsJAaEReRsR27YzddqyGoWTNMhEvC",  # noqa: mock
                                    "value": "399134226.5095641",
                                },
                                "HighNode": "0",
                                "LowLimit": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                                    "value": "0",
                                },
                                "LowNode": "36a5",
                            },
                            "LedgerEntryType": "RippleState",
                            "LedgerIndex": "9DB660A1BF3B982E5A8F4BE0BD4684FEFEBE575741928E67E4EA1DAEA02CA5A6",  # noqa: mock
                            "PreviousFields": {
                                "Balance": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                                    "value": "-197.6826246297997",
                                }
                            },
                            "PreviousTxnID": "BCBB6593A916EDBCC84400948B0525BE7E972B893111FE1C89A7519F8A5ACB2B",  # noqa: mock
                            "PreviousTxnLgrSeq": 88954461,
                        }
                    },
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Balance": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                                    "value": "45.47502732568766",
                                },
                                "Flags": 1114112,
                                "HighLimit": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                                    "value": "0",
                                },
                                "HighNode": "3799",
                                "LowLimit": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                                    "value": "1000000000",
                                },
                                "LowNode": "0",
                            },
                            "LedgerEntryType": "RippleState",
                            "LedgerIndex": "E1C84325F137AD05CB78F59968054BCBFD43CB4E70F7591B6C3C1D1C7E44C6FC",  # noqa: mock
                            "PreviousFields": {
                                "Balance": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                                    "value": "43.2239931744894",
                                }
                            },
                            "PreviousTxnID": "BCBB6593A916EDBCC84400948B0525BE7E972B893111FE1C89A7519F8A5ACB2B",  # noqa: mock
                            "PreviousTxnLgrSeq": 88954461,
                        }
                    },
                ],
                "TransactionIndex": 3,
                "TransactionResult": "tesSUCCESS",
            },
            "hash": "86440061A351FF77F21A24ED045EE958F6256697F2628C3555AEBF29A887518C",  # noqa: mock
            "ledger_index": 88954492,
            "date": 772789130,
        }

        return resp

    def _event_message_limit_order_partially_filled(self):
        resp = {
            "transaction": {
                "Account": "rapido5rxPmP4YkMZZEeXSHqWefxHEkqv6",  # noqa: mock
                "Fee": "10",
                "Flags": 655360,
                "LastLedgerSequence": 88981161,
                "Memos": [
                    {
                        "Memo": {
                            "MemoData": "06574D47B3D98F0D1103815555734BF30D72EC4805086B873FCCD69082FE00903FF7AC1910CF172A3FD5554FBDAD75193FF00068DB8BAC71"  # noqa: mock
                        }
                    }
                ],
                "Sequence": 2368849,
                "SigningPubKey": "EDE30BA017ED458B9B372295863B042C2BA8F11AD53B4BDFB398E778CB7679146B",  # noqa: mock
                "TakerGets": {
                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                    "value": "1.479368155160602",
                },
                "TakerPays": "333",
                "TransactionType": "OfferCreate",
                "TxnSignature": "1165D0B39A5C3C48B65FD20DDF1C0AF544B1413C8B35E6147026F521A8468FB7F8AA3EAA33582A9D8DC9B56E1ED59F6945781118EC4DEC92FF639C3D41C3B402",  # noqa: mock
                "hash": "undefined",
                "ctid": "C54DBEA8001D0000",  # noqa: mock
                "meta": "undefined",
                "validated": "undefined",
                "date": 772789130,
                "ledger_index": "undefined",
                "inLedger": "undefined",
                "metaData": "undefined",
                "status": "undefined",
            },
            "meta": {
                "AffectedNodes": [
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Account": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                                "Balance": "57030924",
                                "Flags": 0,
                                "OwnerCount": 9,
                                "Sequence": 84437901,
                            },
                            "LedgerEntryType": "AccountRoot",
                            "LedgerIndex": "2B3020738E7A44FBDE454935A38D77F12DC5A11E0FA6DAE2D9FCF4719FFAA3BC",  # noqa: mock
                            "PreviousFields": {"Balance": "57364223"},
                            "PreviousTxnID": "1D63D9DFACB8F25ADAF44A1976FBEAF875EF199DEA6F9502B1C6C32ABA8583F6",  # noqa: mock
                            "PreviousTxnLgrSeq": 88981158,
                        }
                    },
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Account": "rapido5rxPmP4YkMZZEeXSHqWefxHEkqv6",  # noqa: mock
                                "AccountTxnID": "602B32630738581F2618849B3338401D381139F8458DDF2D0AC9B61BEED99D70",  # noqa: mock
                                "Balance": "4802538039",
                                "Flags": 0,
                                "OwnerCount": 229,
                                "Sequence": 2368850,
                            },
                            "LedgerEntryType": "AccountRoot",
                            "LedgerIndex": "BFF40FB02870A44349BB5E482CD2A4AA3415C7E72F4D2E9E98129972F26DA9AA",  # noqa: mock
                            "PreviousFields": {
                                "AccountTxnID": "43B7820240604D3AFE46079D91D557259091DDAC17D42CD7688637D58C3B7927",  # noqa: mock
                                "Balance": "4802204750",
                                "Sequence": 2368849,
                            },
                            "PreviousTxnID": "43B7820240604D3AFE46079D91D557259091DDAC17D42CD7688637D58C3B7927",  # noqa: mock
                            "PreviousTxnLgrSeq": 88981160,
                        }
                    },
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Balance": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                                    "value": "41.49115329259071",
                                },
                                "Flags": 1114112,
                                "HighLimit": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                                    "value": "0",
                                },
                                "HighNode": "3799",
                                "LowLimit": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                                    "value": "1000000000",
                                },
                                "LowNode": "0",
                            },
                            "LedgerEntryType": "RippleState",
                            "LedgerIndex": "E1C84325F137AD05CB78F59968054BCBFD43CB4E70F7591B6C3C1D1C7E44C6FC",  # noqa: mock
                            "PreviousFields": {
                                "Balance": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                                    "value": "40.01178513743011",
                                }
                            },
                            "PreviousTxnID": "EA21F8D1CD22FA64C98CB775855F53C186BF0AD24D59728AA8D18340DDAA3C57",  # noqa: mock
                            "PreviousTxnLgrSeq": 88981118,
                        }
                    },
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Balance": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                                    "value": "-5.28497026524528",
                                },
                                "Flags": 2228224,
                                "HighLimit": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rapido5rxPmP4YkMZZEeXSHqWefxHEkqv6",  # noqa: mock
                                    "value": "0",
                                },
                                "HighNode": "18",
                                "LowLimit": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                                    "value": "0",
                                },
                                "LowNode": "387f",
                            },
                            "LedgerEntryType": "RippleState",
                            "LedgerIndex": "E56AB275B511ECDF6E9C9D8BE9404F3FECBE5C841770584036FF8A832AF3F3B9",  # noqa: mock
                            "PreviousFields": {
                                "Balance": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                                    "value": "-6.764486357221399",
                                }
                            },
                            "PreviousTxnID": "43B7820240604D3AFE46079D91D557259091DDAC17D42CD7688637D58C3B7927",  # noqa: mock
                            "PreviousTxnLgrSeq": 88981160,
                        }
                    },
                    {
                        "ModifiedNode": {
                            "FinalFields": {
                                "Account": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                                "BookDirectory": "C73FAC6C294EBA5B9E22A8237AAE80725E85372510A6CA794F0FC4DA2F8AAF5B",  # noqa: mock
                                "BookNode": "0",
                                "Flags": 131072,
                                "OwnerNode": "0",
                                "Sequence": 84437895,
                                "TakerGets": "33",
                                "TakerPays": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                                    "value": "0.000147936815515",
                                },
                            },
                            "LedgerEntryType": "Offer",
                            "LedgerIndex": "F91EFE46023BA559CEF49B670052F19189C8B6422A93FA26D35F2D6A25290D24",  # noqa: mock
                            "PreviousFields": {
                                "TakerGets": "333332",
                                "TakerPays": {
                                    "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                                    "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                                    "value": "1.479516091976118",
                                },
                            },
                            "PreviousTxnID": "12A2F4A0FAA21802E68F4BF78BCA3DE302222B0B9FB938C355EE10E931C151D2",  # noqa: mock
                            "PreviousTxnLgrSeq": 88981157,
                        }
                    },
                ],
                "TransactionIndex": 29,
                "TransactionResult": "tesSUCCESS",
            },
            "hash": "602B32630738581F2618849B3338401D381139F8458DDF2D0AC9B61BEED99D70",  # noqa: mock
            "ledger_index": 88981160,
            "date": 772789130,
        }

        return resp

    def _client_response_account_info(self):
        resp = Response(
            status=ResponseStatus.SUCCESS,
            result={
                "account_data": {
                    "Account": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                    "Balance": "57030864",
                    "Flags": 0,
                    "LedgerEntryType": "AccountRoot",
                    "OwnerCount": 3,
                    "PreviousTxnID": "0E8031892E910EB8F19537610C36E5816D5BABF14C91CF8C73FFE5F5D6A0623E",  # noqa: mock
                    "PreviousTxnLgrSeq": 88981167,
                    "Sequence": 84437907,
                    "index": "2B3020738E7A44FBDE454935A38D77F12DC5A11E0FA6DAE2D9FCF4719FFAA3BC",  # noqa: mock
                },
                "account_flags": {
                    "allowTrustLineClawback": False,
                    "defaultRipple": False,
                    "depositAuth": False,
                    "disableMasterKey": False,
                    "disallowIncomingCheck": False,
                    "disallowIncomingNFTokenOffer": False,
                    "disallowIncomingPayChan": False,
                    "disallowIncomingTrustline": False,
                    "disallowIncomingXRP": False,
                    "globalFreeze": False,
                    "noFreeze": False,
                    "passwordSpent": False,
                    "requireAuthorization": False,
                    "requireDestinationTag": False,
                },
                "ledger_hash": "DFDFA9B7226B8AC1FD909BB9C2EEBDBADF4C37E2C3E283DB02C648B2DC90318C",  # noqa: mock
                "ledger_index": 89003974,
                "validated": True,
            },
            id="account_info_644216",
            type=ResponseType.RESPONSE,
        )

        return resp

    def _client_response_account_objects(self):
        resp = Response(
            status=ResponseStatus.SUCCESS,
            result={
                "account": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                "account_objects": [
                    {
                        "Balance": {
                            "currency": "5553444300000000000000000000000000000000",  # noqa: mock
                            "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                            "value": "2.981957518895808",
                        },
                        "Flags": 1114112,
                        "HighLimit": {
                            "currency": "5553444300000000000000000000000000000000",  # noqa: mock
                            "issuer": "rcEGREd8NmkKRE8GE424sksyt1tJVFZwu",  # noqa: mock
                            "value": "0",
                        },
                        "HighNode": "f9",
                        "LedgerEntryType": "RippleState",
                        "LowLimit": {
                            "currency": "5553444300000000000000000000000000000000",  # noqa: mock
                            "issuer": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",  # noqa: mock
                            "value": "0",
                        },
                        "LowNode": "0",
                        "PreviousTxnID": "C6EFE5E21ABD5F457BFCCE6D5393317B90821F443AD41FF193620E5980A52E71",  # noqa: mock
                        "PreviousTxnLgrSeq": 86277627,
                        "index": "55049B8164998B0566FC5CDB3FC7162280EFE5A84DB9333312D3DFF98AB52380",  # noqa: mock
                    },
                    {
                        "Balance": {
                            "currency": "USD",
                            "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                            "value": "0.011094399237562",
                        },
                        "Flags": 1114112,
                        "HighLimit": {
                            "currency": "USD",
                            "issuer": "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq",
                            "value": "0",
                        },  # noqa: mock
                        "HighNode": "22d3",
                        "LedgerEntryType": "RippleState",
                        "LowLimit": {
                            "currency": "USD",
                            "issuer": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",
                            "value": "0",
                        },  # noqa: mock
                        "LowNode": "0",
                        "PreviousTxnID": "1A9E685EA694157050803B76251C0A6AFFCF1E69F883BF511CF7A85C3AC002B8",  # noqa: mock
                        "PreviousTxnLgrSeq": 85648064,
                        "index": "C510DDAEBFCE83469032E78B9F41D352DABEE2FB454E6982AA5F9D4ECC4D56AA",  # noqa: mock
                    },
                    {
                        "Balance": {
                            "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                            "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",  # noqa: mock
                            "value": "41.49115329259071",
                        },
                        "Flags": 1114112,
                        "HighLimit": {
                            "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                            "issuer": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                            "value": "0",
                        },
                        "HighNode": "3799",
                        "LedgerEntryType": "RippleState",
                        "LowLimit": {
                            "currency": "534F4C4F00000000000000000000000000000000",  # noqa: mock
                            "issuer": "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK",
                            "value": "1000000000",
                        },
                        "LowNode": "0",
                        "PreviousTxnID": "602B32630738581F2618849B3338401D381139F8458DDF2D0AC9B61BEED99D70",  # noqa: mock
                        "PreviousTxnLgrSeq": 88981160,
                        "index": "E1C84325F137AD05CB78F59968054BCBFD43CB4E70F7591B6C3C1D1C7E44C6FC",  # noqa: mock
                    },
                ],
                "ledger_hash": "DFDFA9B7226B8AC1FD909BB9C2EEBDBADF4C37E2C3E283DB02C648B2DC90318C",  # noqa: mock
                "ledger_index": 89003974,
                "limit": 200,
                "validated": True,
            },
            id="account_objects_144811",
            type=ResponseType.RESPONSE,
        )

        return resp

    def _client_response_account_info_issuer(self):
        resp = Response(
            status=ResponseStatus.SUCCESS,
            result={
                "account_data": {
                    "Account": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",  # noqa: mock
                    "Balance": "7329544278",
                    "Domain": "736F6C6F67656E69632E636F6D",  # noqa: mock
                    "EmailHash": "7AC3878BF42A5329698F468A6AAA03B9",  # noqa: mock
                    "Flags": 12058624,
                    "LedgerEntryType": "AccountRoot",
                    "OwnerCount": 0,
                    "PreviousTxnID": "C35579B384BE5DBE064B4778C4EDD18E1388C2CAA2C87BA5122C467265FC7A79",  # noqa: mock
                    "PreviousTxnLgrSeq": 89004092,
                    "RegularKey": "rrrrrrrrrrrrrrrrrrrrBZbvji",
                    "Sequence": 14,
                    "TransferRate": 1000100000,
                    "index": "ED3EE6FAB9822943809FBCBEEC44F418D76292A355B38C1224A378AEB3A65D6D",  # noqa: mock
                    "urlgravatar": "http://www.gravatar.com/avatar/7ac3878bf42a5329698f468a6aaa03b9",  # noqa: mock
                },
                "account_flags": {
                    "allowTrustLineClawback": False,
                    "defaultRipple": True,
                    "depositAuth": False,
                    "disableMasterKey": True,
                    "disallowIncomingCheck": False,
                    "disallowIncomingNFTokenOffer": False,
                    "disallowIncomingPayChan": False,
                    "disallowIncomingTrustline": False,
                    "disallowIncomingXRP": True,
                    "globalFreeze": False,
                    "noFreeze": True,
                    "passwordSpent": False,
                    "requireAuthorization": False,
                    "requireDestinationTag": False,
                },
                "ledger_hash": "AE78A574FCD1B45135785AC9FB64E7E0E6E4159821EF0BB8A59330C1B0E047C9",  # noqa: mock
                "ledger_index": 89004663,
                "validated": True,
            },
            id="account_info_73967",
            type=ResponseType.RESPONSE,
        )

        return resp

    def test_get_new_order_book_successful(self):
        self.async_run_with_timeout(self.connector._orderbook_ds.get_new_order_book(self.trading_pair))
        order_book: OrderBook = self.connector.get_order_book(self.trading_pair)

        bids = list(order_book.bid_entries())
        asks = list(order_book.ask_entries())
        self.assertEqual(2, len(bids))
        self.assertEqual(0.2235426870065409, bids[0].price)
        self.assertEqual(836.5292665312212, bids[0].amount)
        self.assertEqual(2, len(asks))
        self.assertEqual(0.22452700389932698, asks[0].price)
        self.assertEqual(91.846106, asks[0].amount)

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._verify_transaction_result")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_autofill")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_sign")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_submit")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_place_limit_order(
        self,
        network_mock,
        process_order_update_mock,
        submit_mock,
        sign_mock,
        autofill_mock,
        verify_transaction_result_mock,
    ):
        autofill_mock.return_value = {}
        verify_transaction_result_mock.return_value = True, {}
        sign_mock.return_value = Transaction(
            sequence=1, last_ledger_sequence=1, account="r1234", transaction_type=TransactionType.OFFER_CREATE
        )

        submit_mock.return_value = Response(
            status=ResponseStatus.SUCCESS, result={"engine_result": "tesSUCCESS", "engine_result_message": "something"}
        )

        self.async_run_with_timeout(
            self.connector._place_order(
                "hbot",
                self.trading_pair,
                Decimal("12345.12345678901234567"),
                TradeType.BUY,
                OrderType.LIMIT,
                Decimal("1"),
            )
        )

        self.async_run_with_timeout(
            self.connector._place_order(
                "hbot",
                self.trading_pair,
                Decimal("12345.12345678901234567"),
                TradeType.SELL,
                OrderType.LIMIT,
                Decimal("1234567.123456789"),
            )
        )

        self.async_run_with_timeout(
            self.connector._place_order(
                "hbot",
                self.trading_pair_usd,
                Decimal("12345.12345678901234567"),
                TradeType.BUY,
                OrderType.LIMIT,
                Decimal("1234567.123456789"),
            )
        )

        self.async_run_with_timeout(
            self.connector._place_order(
                "hbot",
                self.trading_pair_usd,
                Decimal("12345.12345678901234567"),
                TradeType.SELL,
                OrderType.LIMIT,
                Decimal("1234567.123456789"),
            )
        )

        self.assertTrue(network_mock.called)
        self.assertTrue(process_order_update_mock.called)
        self.assertTrue(verify_transaction_result_mock.called)
        self.assertTrue(submit_mock.called)
        self.assertTrue(autofill_mock.called)
        self.assertTrue(sign_mock.called)

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._verify_transaction_result")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_autofill")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_sign")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_submit")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_place_market_order(
        self,
        network_mock,
        process_order_update_mock,
        submit_mock,
        sign_mock,
        autofill_mock,
        verify_transaction_result_mock,
    ):
        autofill_mock.return_value = {}
        verify_transaction_result_mock.return_value = True, {}
        sign_mock.return_value = Transaction(
            sequence=1, last_ledger_sequence=1, account="r1234", transaction_type=TransactionType.OFFER_CREATE
        )

        submit_mock.return_value = Response(
            status=ResponseStatus.SUCCESS, result={"engine_result": "tesSUCCESS", "engine_result_message": "something"}
        )

        self.async_run_with_timeout(
            self.connector._place_order(
                "hbot", self.trading_pair, Decimal("1"), TradeType.BUY, OrderType.MARKET, Decimal("1")
            )
        )

        self.async_run_with_timeout(
            self.connector._place_order(
                "hbot", self.trading_pair, Decimal("1"), TradeType.SELL, OrderType.MARKET, Decimal("1")
            )
        )

        self.async_run_with_timeout(
            self.connector._place_order(
                "hbot", self.trading_pair_usd, Decimal("1"), TradeType.BUY, OrderType.MARKET, Decimal("1")
            )
        )

        self.async_run_with_timeout(
            self.connector._place_order(
                "hbot", self.trading_pair_usd, Decimal("1"), TradeType.SELL, OrderType.MARKET, Decimal("1")
            )
        )

        self.assertTrue(network_mock.called)
        self.assertTrue(process_order_update_mock.called)
        self.assertTrue(verify_transaction_result_mock.called)
        self.assertTrue(submit_mock.called)
        self.assertTrue(autofill_mock.called)
        self.assertTrue(sign_mock.called)

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.autofill", new_callable=MagicMock)
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.submit", new_callable=MagicMock)
    def test_place_order_exception_handling_not_found_market(self, submit_mock, autofill_mock):
        with self.assertRaises(Exception) as context:
            self.async_run_with_timeout(
                self.connector._place_order(
                    order_id="test_order",
                    trading_pair="NOT_FOUND",
                    amount=Decimal("1.0"),
                    trade_type=TradeType.BUY,
                    order_type=OrderType.MARKET,
                    price=Decimal("1"),
                )
            )

        # Verify the exception was raised and contains the expected message
        self.assertTrue("Market NOT_FOUND not found in markets list" in str(context.exception))

        # Ensure the submit method was not called due to the exception in autofill
        submit_mock.assert_not_called()

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.autofill", new_callable=MagicMock)
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.submit", new_callable=MagicMock)
    def test_place_order_exception_handling_autofill(self, submit_mock, autofill_mock):
        # Simulate an exception during the autofill operation
        autofill_mock.side_effect = Exception("Test exception during autofill")

        with self.assertRaises(Exception) as context:
            self.async_run_with_timeout(
                self.connector._place_order(
                    order_id="test_order",
                    trading_pair="SOLO-XRP",
                    amount=Decimal("1.0"),
                    trade_type=TradeType.BUY,
                    order_type=OrderType.MARKET,
                    price=Decimal("1"),
                )
            )

        # Verify the exception was raised and contains the expected message
        self.assertTrue(
            "Order None (test_order) creation failed: Test exception during autofill" in str(context.exception)
        )

        # Ensure the submit method was not called due to the exception in autofill
        submit_mock.assert_not_called()

    @patch("hummingbot.connector.exchange_py_base.ExchangePyBase._sleep")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._verify_transaction_result")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_autofill")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_sign")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_submit")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_place_order_exception_handling_failed_verify(
        self,
        network_mock,
        process_order_update_mock,
        submit_mock,
        sign_mock,
        autofill_mock,
        verify_transaction_result_mock,
        sleep_mock,
    ):
        autofill_mock.return_value = {}
        verify_transaction_result_mock.return_value = False, {}
        sign_mock.return_value = Transaction(
            sequence=1, last_ledger_sequence=1, account="r1234", transaction_type=TransactionType.OFFER_CREATE
        )

        submit_mock.return_value = Response(
            status=ResponseStatus.SUCCESS, result={"engine_result": "tesSUCCESS", "engine_result_message": "something"}
        )

        with self.assertRaises(Exception) as context:
            self.async_run_with_timeout(
                self.connector._place_order(
                    "hbot",
                    self.trading_pair_usd,
                    Decimal("12345.12345678901234567"),
                    TradeType.SELL,
                    OrderType.LIMIT,
                    Decimal("1234567.123456789"),
                )
            )

        # # Verify the exception was raised and contains the expected message
        self.assertTrue(
            "Order 1-1 (hbot) creation failed: Failed to verify transaction result for order hbot (1-1)"
            in str(context.exception)
        )

    @patch("hummingbot.connector.exchange_py_base.ExchangePyBase._sleep")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._verify_transaction_result")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_autofill")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_sign")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_submit")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_place_order_exception_handling_none_verify_resp(
        self,
        network_mock,
        process_order_update_mock,
        submit_mock,
        sign_mock,
        autofill_mock,
        verify_transaction_result_mock,
        sleep_mock,
    ):
        autofill_mock.return_value = {}
        verify_transaction_result_mock.return_value = False, None
        sign_mock.return_value = Transaction(
            sequence=1, last_ledger_sequence=1, account="r1234", transaction_type=TransactionType.OFFER_CREATE
        )

        submit_mock.return_value = Response(
            status=ResponseStatus.SUCCESS, result={"engine_result": "tesSUCCESS", "engine_result_message": "something"}
        )

        with self.assertRaises(Exception) as context:
            self.async_run_with_timeout(
                self.connector._place_order(
                    "hbot",
                    self.trading_pair_usd,
                    Decimal("12345.12345678901234567"),
                    TradeType.SELL,
                    OrderType.LIMIT,
                    Decimal("1234567.123456789"),
                )
            )

        # # Verify the exception was raised and contains the expected message
        self.assertTrue("Order 1-1 (hbot) creation failed: Failed to place order hbot (1-1)" in str(context.exception))

    @patch("hummingbot.connector.exchange_py_base.ExchangePyBase._sleep")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._verify_transaction_result")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_autofill")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_sign")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_submit")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_place_order_exception_handling_failed_submit(
        self,
        network_mock,
        process_order_update_mock,
        submit_mock,
        sign_mock,
        autofill_mock,
        verify_transaction_result_mock,
        sleep_mock,
    ):
        autofill_mock.return_value = {}
        verify_transaction_result_mock.return_value = False, None
        sign_mock.return_value = Transaction(
            sequence=1, last_ledger_sequence=1, account="r1234", transaction_type=TransactionType.OFFER_CREATE
        )

        submit_mock.return_value = Response(
            status=ResponseStatus.ERROR, result={"engine_result": "tec", "engine_result_message": "something"}
        )

        with self.assertRaises(Exception) as context:
            self.async_run_with_timeout(
                self.connector._place_order(
                    "hbot",
                    self.trading_pair_usd,
                    Decimal("12345.12345678901234567"),
                    TradeType.SELL,
                    OrderType.LIMIT,
                    Decimal("1234567.123456789"),
                )
            )

        print(str(context.exception))

        # # Verify the exception was raised and contains the expected message
        self.assertTrue("Order 1-1 (hbot) creation failed: Failed to place order hbot (1-1)" in str(context.exception))

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_autofill")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_sign")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_submit")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_place_cancel(
        self,
        network_mock,
        submit_mock,
        sign_mock,
        autofill_mock,
    ):
        autofill_mock.return_value = {}
        sign_mock.return_value = Transaction(
            sequence=1, last_ledger_sequence=1, account="r1234", transaction_type=TransactionType.OFFER_CREATE
        )

        submit_mock.return_value = Response(
            status=ResponseStatus.SUCCESS, result={"engine_result": "tesSUCCESS", "engine_result_message": "something"}
        )

        in_flight_order = InFlightOrder(
            client_order_id="hbot",
            exchange_order_id="1234-4321",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1"),
            creation_timestamp=1,
        )

        self.async_run_with_timeout(self.connector._place_cancel("hbot", tracked_order=in_flight_order))
        self.assertTrue(network_mock.called)
        self.assertTrue(submit_mock.called)
        self.assertTrue(autofill_mock.called)
        self.assertTrue(sign_mock.called)

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._verify_transaction_result")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_autofill")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_sign")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_submit")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_trade_update")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.process_trade_fills")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._request_order_status")
    def test_place_order_and_process_update(
        self,
        request_order_status_mock,
        process_trade_fills_mock,
        process_trade_update_mock,
        network_mock,
        process_order_update_mock,
        submit_mock,
        sign_mock,
        autofill_mock,
        verify_transaction_result_mock,
    ):
        request_order_status_mock.return_value = OrderUpdate(
            trading_pair=self.trading_pair,
            new_state=OrderState.FILLED,
            update_timestamp=1,
        )
        autofill_mock.return_value = {}
        verify_transaction_result_mock.return_value = True, Response(
            status=ResponseStatus.SUCCESS, result={"engine_result": "tesSUCCESS", "engine_result_message": "something"}
        )
        sign_mock.return_value = Transaction(
            sequence=1, last_ledger_sequence=1, account="r1234", transaction_type=TransactionType.OFFER_CREATE
        )

        submit_mock.return_value = Response(
            status=ResponseStatus.SUCCESS, result={"engine_result": "tesSUCCESS", "engine_result_message": "something"}
        )

        in_flight_order = InFlightOrder(
            client_order_id="hbot",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1"),
            price=Decimal("1"),
            creation_timestamp=1,
        )

        exchange_order_id = self.async_run_with_timeout(
            self.connector._place_order_and_process_update(order=in_flight_order)
        )
        self.assertTrue(network_mock.called)
        self.assertTrue(submit_mock.called)
        self.assertTrue(autofill_mock.called)
        self.assertTrue(sign_mock.called)
        self.assertTrue(process_order_update_mock.called)
        self.assertTrue(process_trade_update_mock.called)
        self.assertTrue(process_trade_fills_mock.called)
        self.assertEqual("1-1", exchange_order_id)

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._verify_transaction_result")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_autofill")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_sign")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.tx_submit")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._request_order_status")
    def test_execute_order_cancel_and_process_update(
        self,
        request_order_status_mock,
        network_mock,
        process_order_update_mock,
        submit_mock,
        sign_mock,
        autofill_mock,
        verify_transaction_result_mock,
    ):
        request_order_status_mock.return_value = OrderUpdate(
            trading_pair=self.trading_pair,
            new_state=OrderState.FILLED,
            update_timestamp=1,
        )
        autofill_mock.return_value = {}
        verify_transaction_result_mock.return_value = True, Response(
            status=ResponseStatus.SUCCESS,
            result={"engine_result": "tesSUCCESS", "engine_result_message": "something", "meta": {"AffectedNodes": []}},
        )
        sign_mock.return_value = Transaction(
            sequence=1, last_ledger_sequence=1, account="r1234", transaction_type=TransactionType.OFFER_CREATE
        )

        submit_mock.return_value = Response(
            status=ResponseStatus.SUCCESS, result={"engine_result": "tesSUCCESS", "engine_result_message": "something"}
        )

        in_flight_order = InFlightOrder(
            client_order_id="hbot",
            exchange_order_id="1234-4321",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1"),
            price=Decimal("1"),
            creation_timestamp=1,
        )

        result = self.async_run_with_timeout(
            self.connector._execute_order_cancel_and_process_update(order=in_flight_order)
        )
        self.assertTrue(network_mock.called)
        self.assertTrue(submit_mock.called)
        self.assertTrue(autofill_mock.called)
        self.assertTrue(sign_mock.called)
        self.assertTrue(process_order_update_mock.called)
        self.assertTrue(result)

    def test_format_trading_rules(self):
        trading_rules_info = {"XRP-USD": {"base_tick_size": 8, "quote_tick_size": 8, "minimum_order_size": 0.01}}

        result = self.connector._format_trading_rules(trading_rules_info)

        expected_result = [
            TradingRule(
                trading_pair="XRP-USD",
                min_order_size=Decimal(0.01),
                min_price_increment=Decimal("1e-8"),
                min_quote_amount_increment=Decimal("1e-8"),
                min_base_amount_increment=Decimal("1e-8"),
                min_notional_size=Decimal("1e-8"),
            )
        ]

        self.assertEqual(result[0].min_order_size, expected_result[0].min_order_size)
        self.assertEqual(result[0].min_price_increment, expected_result[0].min_price_increment)
        self.assertEqual(result[0].min_quote_amount_increment, expected_result[0].min_quote_amount_increment)
        self.assertEqual(result[0].min_base_amount_increment, expected_result[0].min_base_amount_increment)
        self.assertEqual(result[0].min_notional_size, expected_result[0].min_notional_size)

    def test_format_trading_pair_fee_rules(self):
        trading_rules_info = {"XRP-USD": {"base_transfer_rate": 0.01, "quote_transfer_rate": 0.01}}

        result = self.connector._format_trading_pair_fee_rules(trading_rules_info)

        expected_result = [
            {
                "trading_pair": "XRP-USD",
                "base_token": "XRP",
                "quote_token": "USD",
                "base_transfer_rate": 0.01,
                "quote_transfer_rate": 0.01,
            }
        ]

        self.assertEqual(result, expected_result)

    @patch("hummingbot.connector.exchange_py_base.ExchangePyBase._iter_user_event_queue")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.get_order_by_sequence")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_auth.XRPLAuth.get_account")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._update_balances")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    def test_user_stream_event_listener(
        self,
        process_order_update_mock,
        update_balances_mock,
        get_account_mock,
        get_order_by_sequence,
        iter_user_event_queue_mock,
    ):
        async def async_generator(lst):
            for item in lst:
                yield item

        message_list = [self._event_message()]
        async_iterable = async_generator(message_list)

        in_flight_order = InFlightOrder(
            client_order_id="hbot",
            exchange_order_id="84437780-88954510",
            trading_pair=self.trading_pair,
            order_type=OrderType.MARKET,
            trade_type=TradeType.BUY,
            amount=Decimal("2.239836701211152"),
            price=Decimal("0.224547537"),
            creation_timestamp=1,
        )

        iter_user_event_queue_mock.return_value = async_iterable
        get_order_by_sequence.return_value = in_flight_order
        get_account_mock.return_value = "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK"  # noqa: mock

        self.async_run_with_timeout(self.connector._user_stream_event_listener())
        self.assertTrue(update_balances_mock.called)
        self.assertTrue(get_account_mock.called)
        self.assertTrue(get_order_by_sequence.called)
        self.assertTrue(iter_user_event_queue_mock.called)

        args, kwargs = process_order_update_mock.call_args
        self.assertEqual(kwargs["order_update"].new_state, OrderState.FILLED)

    @patch("hummingbot.connector.exchange_py_base.ExchangePyBase._iter_user_event_queue")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.get_order_by_sequence")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_auth.XRPLAuth.get_account")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._update_balances")
    @patch("hummingbot.connector.client_order_tracker.ClientOrderTracker.process_order_update")
    def test_user_stream_event_listener_partially_filled(
        self,
        process_order_update_mock,
        update_balances_mock,
        get_account_mock,
        get_order_by_sequence,
        iter_user_event_queue_mock,
    ):
        async def async_generator(lst):
            for item in lst:
                yield item

        message_list = [self._event_message_limit_order_partially_filled()]
        async_iterable = async_generator(message_list)

        in_flight_order = InFlightOrder(
            client_order_id="hbot",
            exchange_order_id="84437895-88954510",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1.47951609"),
            price=Decimal("0.224547537"),
            creation_timestamp=1,
        )

        iter_user_event_queue_mock.return_value = async_iterable
        get_order_by_sequence.return_value = in_flight_order
        get_account_mock.return_value = "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK"  # noqa: mock

        self.async_run_with_timeout(self.connector._user_stream_event_listener())
        self.assertTrue(update_balances_mock.called)
        self.assertTrue(get_account_mock.called)
        self.assertTrue(get_order_by_sequence.called)
        self.assertTrue(iter_user_event_queue_mock.called)

        args, kwargs = process_order_update_mock.call_args
        self.assertEqual(kwargs["order_update"].new_state, OrderState.PARTIALLY_FILLED)

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_auth.XRPLAuth.get_account")
    def test_update_balances(self, get_account_mock, network_mock):
        get_account_mock.return_value = "r2XdzWFVoHGfGVmXugtKhxMu3bqhsYiWK"  # noqa: mock

        def side_effect_function(arg: Request):
            if arg.method == RequestMethod.ACCOUNT_INFO:
                return self._client_response_account_info()
            elif arg.method == RequestMethod.ACCOUNT_OBJECTS:
                return self._client_response_account_objects()
            else:
                raise ValueError("Invalid method")

        self.connector._xrpl_client.request.side_effect = side_effect_function

        self.async_run_with_timeout(self.connector._update_balances())

        self.assertTrue(network_mock.called)
        self.assertTrue(get_account_mock.called)

        self.assertEqual(self.connector._account_balances["XRP"], Decimal("57.030864"))
        self.assertEqual(self.connector._account_balances["USD"], Decimal("0.011094399237562"))
        self.assertEqual(self.connector._account_balances["SOLO"], Decimal("41.49115329259071"))

        self.assertEqual(self.connector._account_available_balances["XRP"], Decimal("41.030864"))
        self.assertEqual(self.connector._account_available_balances["USD"], Decimal("0.011094399237562"))
        self.assertEqual(self.connector._account_available_balances["SOLO"], Decimal("41.49115329259071"))

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_make_trading_rules_request(self, network_mock):
        def side_effect_function(arg: Request):
            if arg.method == RequestMethod.ACCOUNT_INFO:
                return self._client_response_account_info_issuer()
            else:
                raise ValueError("Invalid method")

        self.connector._xrpl_client.request.side_effect = side_effect_function

        result = self.async_run_with_timeout(self.connector._make_trading_rules_request())

        self.assertTrue(network_mock.called)
        self.assertEqual(
            result["SOLO-XRP"]["base_currency"].currency, "534F4C4F00000000000000000000000000000000"
        )  # noqa: mock
        self.assertEqual(result["SOLO-XRP"]["base_currency"].issuer, "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz")  # noqa: mock
        self.assertEqual(result["SOLO-XRP"]["base_tick_size"], 15)
        self.assertEqual(result["SOLO-XRP"]["quote_tick_size"], 6)
        self.assertEqual(result["SOLO-XRP"]["base_transfer_rate"], 9.999999999998899e-05)
        self.assertEqual(result["SOLO-XRP"]["quote_transfer_rate"], 0)
        self.assertEqual(result["SOLO-XRP"]["minimum_order_size"], 1e-06)

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.wait_for_final_transaction_outcome")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_verify_transaction_success(self, network_check_mock, wait_for_outcome_mock):
        wait_for_outcome_mock.return_value = Response(status=ResponseStatus.SUCCESS, result={})
        transaction_mock = MagicMock()
        transaction_mock.get_hash.return_value = "hash"
        transaction_mock.last_ledger_sequence = 12345

        result, response = self.async_run_with_timeout(
            self.connector._verify_transaction_result({"transaction": transaction_mock, "prelim_result": "tesSUCCESS"})
        )
        self.assertTrue(result)
        self.assertIsNotNone(response)

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.wait_for_final_transaction_outcome")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_verify_transaction_exception(self, network_check_mock, wait_for_outcome_mock):
        wait_for_outcome_mock.side_effect = Exception("Test exception")
        transaction_mock = MagicMock()
        transaction_mock.get_hash.return_value = "hash"
        transaction_mock.last_ledger_sequence = 12345

        with self.assertLogs(level="ERROR") as log:
            result, response = self.async_run_with_timeout(
                self.connector._verify_transaction_result(
                    {"transaction": transaction_mock, "prelim_result": "tesSUCCESS"}
                )
            )

        log_output = log.output[0]
        self.assertEqual(
            log_output,
            "ERROR:hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange:Submitted transaction failed: Test exception",
        )

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.wait_for_final_transaction_outcome")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_verify_transaction_exception_none_transaction(self, network_check_mock, wait_for_outcome_mock):
        wait_for_outcome_mock.side_effect = Exception("Test exception")

        with self.assertLogs(level="ERROR") as log:
            result, response = self.async_run_with_timeout(
                self.connector._verify_transaction_result({"transaction": None, "prelim_result": "tesSUCCESS"})
            )

        log_output = log.output[0]
        self.assertEqual(
            log_output,
            "ERROR:hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange:Failed to verify transaction result, transaction is None",
        )

    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange.wait_for_final_transaction_outcome")
    @patch("hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange._make_network_check_request")
    def test_verify_transaction_exception_none_prelim(self, network_check_mock, wait_for_outcome_mock):
        wait_for_outcome_mock.side_effect = Exception("Test exception")
        transaction_mock = MagicMock()
        transaction_mock.get_hash.return_value = "hash"
        transaction_mock.last_ledger_sequence = 12345

        with self.assertLogs(level="ERROR") as log:
            result, response = self.async_run_with_timeout(
                self.connector._verify_transaction_result({"transaction": transaction_mock, "prelim_result": None})
            )

        log_output = log.output[0]
        self.assertEqual(
            log_output,
            "ERROR:hummingbot.connector.exchange.xrpl.xrpl_exchange.XrplExchange:Failed to verify transaction result, prelim_result is None",
        )

    def test_get_order_by_sequence_order_found(self):
        # Setup
        sequence = "84437895"
        order = InFlightOrder(
            client_order_id="hbot",
            exchange_order_id="84437895-88954510",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1.47951609"),
            price=Decimal("0.224547537"),
            creation_timestamp=1,
        )

        self.connector._order_tracker = MagicMock()
        self.connector._order_tracker.all_fillable_orders = {"test_order": order}

        # Action
        result = self.connector.get_order_by_sequence(sequence)

        # Assert
        self.assertIsNotNone(result)
        self.assertEqual(result.client_order_id, "hbot")

    def test_get_order_by_sequence_order_not_found(self):
        # Setup
        sequence = "100"

        # Action
        result = self.connector.get_order_by_sequence(sequence)

        # Assert
        self.assertIsNone(result)

    def test_get_order_by_sequence_order_without_exchange_id(self):
        # Setup
        order = InFlightOrder(
            client_order_id="test_order",
            trading_pair="XRP_USD",
            amount=Decimal("1.47951609"),
            price=Decimal("0.224547537"),
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            exchange_order_id=None,
            creation_timestamp=1,
        )

        self.connector._order_tracker = MagicMock()
        self.connector._order_tracker.all_fillable_orders = {"test_order": order}

        # Action
        result = self.connector.get_order_by_sequence("100")

        # Assert
        self.assertIsNone(result)
