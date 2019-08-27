from pybtc.functions.tools import rh2s, s2rh
from pybtc.functions.tools import map_into_range
from pybtc.functions.hash import siphash
from pybtc.functions.filters  import create_gcs
from pybtc.connector.block_loader import BlockLoader
from pybtc.functions.block import merkle_tree, merkle_proof
from pybtc.connector.utxo import UTXO, UUTXO
from pybtc.connector.utils import decode_block_tx
from pybtc.connector.utils import Cache
from pybtc.connector.utils import seconds_to_age
from pybtc.transaction import Transaction
from pybtc import int_to_bytes, bytes_to_int, bytes_from_hex
from pybtc import MRU, parse_script, MINER_COINBASE_TAG, MINER_PAYOUT_TAG, hash_to_address
from collections import deque
import traceback
import json



import asyncio
import time
from math import *
from _pickle import loads

try:
    import aiojsonrpc
except:
    pass

try:
    import zmq
    import zmq.asyncio
except:
    pass

try:
    import asyncpg
except:
    pass

class Connector:

    def __init__(self, node_rpc_url, node_zerromq_url, logger,
                 last_block_height=0, chain_tail=None,
                 tx_handler=None, orphan_handler=None,
                 before_block_handler=None, block_handler=None, after_block_handler=None,
                 block_batch_handler=None,
                 flush_app_caches_handler=None,
                 synchronization_completed_handler=None,
                 block_timeout=30,
                 deep_sync_limit=100, backlog=0, mempool_tx=True,
                 rpc_batch_limit=50, rpc_threads_limit=100, rpc_timeout=100,
                 utxo_data=False,
                 utxo_cache_size=1000000,
                 tx_orphan_buffer_limit=1000,
                 skip_opreturn=True,
                 block_filters=False,
                 option_block_filter_fps=54975581,
                 option_block_filter_bits=25,
                 merkle_proof=False,
                 tx_map=False,
                 analytica=False,
                 block_cache_workers= 4,
                 block_preload_cache_limit= 1000 * 1000000,
                 block_preload_batch_size_limit = 200000000,
                 block_hashes_cache_limit= 200 * 1000000,
                 db_type=None,
                 db=None,
                 app_proc_title="Connector"):



        self.loop = asyncio.get_event_loop()

        # settings
        self.log = logger
        self.rpc = None
        self.rpc_url = node_rpc_url
        self.app_proc_title = app_proc_title
        self.rpc_timeout = rpc_timeout
        self.rpc_batch_limit = rpc_batch_limit
        self.zmq_url = node_zerromq_url
        self.orphan_handler = orphan_handler
        self.block_timeout = block_timeout
        self.tx_handler = tx_handler
        self.skip_opreturn = skip_opreturn
        self.option_block_filters = block_filters
        self.option_block_filter_fps = option_block_filter_fps
        self.option_block_filter_bits = option_block_filter_bits
        self.option_merkle_proof = merkle_proof
        self.option_tx_map = tx_map
        self.option_analytica = analytica
        self.before_block_handler = before_block_handler
        self.block_handler = block_handler
        self.after_block_handler = after_block_handler
        self.block_batch_handler = block_batch_handler
        self.flush_app_caches_handler = flush_app_caches_handler
        self.synchronization_completed_handler = synchronization_completed_handler
        self.block_preload_batch_size_limit = block_preload_batch_size_limit
        self.deep_sync_limit = deep_sync_limit
        self.backlog = backlog
        self.mempool_tx = mempool_tx
        self.tx_orphan_buffer_limit = tx_orphan_buffer_limit
        self.db_type = db_type
        self.db = db
        self.utxo_cache_size = utxo_cache_size
        self.block_cache_workers = block_cache_workers
        self.utxo_data = utxo_data
        self.chain_tail = list(chain_tail) if chain_tail else []


        # state and stats
        self.node_last_block = None
        self.sync_utxo = None
        self.uutxo = None
        self.cache_loading = False
        self.app_block_height_on_start = int(last_block_height) if int(last_block_height) else 0
        self.last_block_height = -1
        self.last_block_utxo_cached_height = 0
        self.deep_synchronization = False

        self.block_dependency_tx = 0 # counter of tx that have dependencies in block
        self.active = True
        self.get_next_block_mutex = False
        self.active_block = asyncio.Future()
        self.active_block.set_result(True)
        self.last_zmq_msg = int(time.time())
        self.total_received_tx = 0
        self.total_received_tx_stat = 0
        self.blocks_processed_count = 0
        self.blocks_decode_time = 0
        self.blocks_download_time = 0
        self.blocks_processing_time = 0
        self.tx_processing_time = 0
        self.non_cached_blocks = 0
        self.total_received_tx_time = 0
        self.coins = 0
        self.op_return = 0
        self.destroyed_coins = 0
        self.preload_cached_total = 0
        self.preload_cached = 0
        self.preload_cached_annihilated = 0
        self.start_time = time.time()
        self.total_received_tx_last = 0
        self.start_time_last = time.time()
        self.batch_time = time.time()
        self.batch_load_utxo = 0
        self.batch_parsing = 0
        self.batch_handler = 0
        self.app_last_block = None
        # cache and system
        self.block_preload_cache_limit = block_preload_cache_limit
        self.block_hashes_cache_limit = block_hashes_cache_limit
        self.tx_cache_limit = 144 * 5000
        self.block_headers_cache_limit = 100 * 100000
        self.block_preload = Cache(max_size=self.block_preload_cache_limit, clear_tail=False)
        self.block_hashes = Cache(max_size=self.block_hashes_cache_limit)
        self.block_hashes_preload_mutex = False
        self.tx_cache = MRU(self.tx_cache_limit)
        self.tx_orphan_buffer = MRU()
        self.new_tx = MRU()
        self.tx_orphan_resolved = 0
        self.block_headers_cache = Cache(max_size=self.block_headers_cache_limit)
        self.chain_tail_start_len = len(chain_tail)
        self.mempool_tx_count = 0


        self.block_txs_request = asyncio.Future()
        self.block_txs_request.set_result(True)
        self.new_tx_handler = None
        self.new_tx_tasks = 0

        self.await_tx = list()
        self.missed_tx = list()
        self.await_tx_future = dict()
        self.add_tx_future = dict()
        self.get_missed_tx_threads = 0
        self.synchronized = False
        self.get_missed_tx_threads_limit = rpc_threads_limit
        self.tx_in_process = set()
        self.zmqContext = None
        self.tasks = list()
        self.unconfirmed_tx_processing = asyncio.Future()
        self.unconfirmed_tx_processing.set_result(True)

        self.log.info("Node connector started")
        self.connected = self.loop.create_task(self.start())



    async def start(self):
        if self.utxo_data:
            await self.utxo_init()
        else:
            self.last_block_height = self.app_block_height_on_start

        while True:
            self.log.info("Connector initialization")
            try:
                self.rpc = aiojsonrpc.rpc(self.rpc_url, self.loop, timeout=self.rpc_timeout)
                self.node_last_block = await self.rpc.getblockcount()
            except Exception as err:
                self.log.error("Get node best block error:" + str(err))
            if not isinstance(self.node_last_block, int):
                self.log.error("Get node best block height failed")
                self.log.error("Node rpc url: " + self.rpc_url)
                await asyncio.sleep(10)
                continue

            self.log.info("Node best block height %s" % self.node_last_block)
            self.log.info("Connector last block height %s [%s]" % (self.last_block_height,
                                                                   self.last_block_utxo_cached_height))
            self.log.info("Application last block height %s" % self.app_block_height_on_start)

            if self.node_last_block < self.last_block_height:
                self.log.error("Node is behind application blockchain state!")
                await asyncio.sleep(10)
                continue
            elif self.node_last_block == self.last_block_height:
                self.log.info("Blockchain is synchronized")
            else:
                d = self.node_last_block - self.last_block_height
                self.log.info("%s blocks before synchronization" % d)
                if d > self.deep_sync_limit:
                    self.log.info("Deep synchronization mode")
                    self.deep_synchronization = True
            break

        if self.utxo_data:
            if self.db_type == "postgresql":
                db = self.db_pool
            else:
                db = self.db
            self.sync_utxo = UTXO(self.db_type, db, self.rpc, self.loop, self.log, self.utxo_cache_size)
            self.uutxo = UUTXO(self.db_type, db, self.log)


        h = self.last_block_height
        # if h < len(self.chain_tail):
        #     raise Exception("Chain tail len not match last block height")
        for row in reversed(self.chain_tail):
            self.block_headers_cache.set(row, h)
            h -= 1
        if self.utxo_data and self.db_type == "postgresql":
            self.block_loader = BlockLoader(self, workers=self.block_cache_workers, dsn = self.db)
        else:
            self.block_loader = BlockLoader(self,workers = self.block_cache_workers)
        self.zeromq_task = self.loop.create_task(self.zeromq_handler())
        self.tasks.append(self.loop.create_task(self.watchdog()))
        self.get_next_block_mutex = True
        self.loop.create_task(self.get_next_block())


    async def utxo_init(self):
        if self.db_type is None:
            raise Exception("UTXO data required  db connection")
        if self.db_type != "postgresql":
            raise Exception("Connector supported database engine is: postgresql")
        # if self.db_type not in ("rocksdb", "leveldb", "postgresql"):
        #     raise Exception("Connector supported database types is: rocksdb, leveldb, postgresql")
        if self.db_type in ("rocksdb", "leveldb"):
            # rocksdb and leveldb
            lb = self.db.get(b"last_block")
            if lb is None:
                lb = 0
                self.db.put(b"last_block", int_to_bytes(0))
                self.db.put(b"last_cached_block", int_to_bytes(0))
            else:
                lb = bytes_to_int(lb)
            lc = bytes_to_int(self.db.get(b"last_cached_block"))
        else:
            # postgresql
            self.db_pool = await asyncpg.create_pool(dsn=self.db, min_size=1, max_size=20)
            async with self.db_pool.acquire() as conn:
                await conn.execute("""CREATE TABLE IF NOT EXISTS 
                                          connector_utxo (outpoint BYTEA,
                                                          pointer BIGINT,
                                                          address BYTEA,
                                                          amount  BIGINT,
                                                          PRIMARY KEY(outpoint));
                                   """)
                await conn.execute("""CREATE TABLE IF NOT EXISTS 
                                          connector_unconfirmed_utxo (outpoint BYTEA,
                                                                      out_tx_id BYTEA,
                                                                      address BYTEA,
                                                                      amount  BIGINT,
                                                                      PRIMARY KEY (outpoint));                                                      
                                   """)
                await conn.execute("""CREATE TABLE IF NOT EXISTS 
                                          connector_unconfirmed_stxo (outpoint BYTEA, 
                                                                      sequence  INT,
                                                                      out_tx_id BYTEA,
                                                                      tx_id BYTEA,
                                                                      input_index INT,
                                                                      address BYTEA,
                                                                      PRIMARY KEY(outpoint, sequence));                                                      
                                   """)

                await conn.execute("""CREATE TABLE IF NOT EXISTS 
                                          connector_block_state_checkpoint (height  INT,
                                                                            data BYTEA,
                                                                            PRIMARY KEY (height));                                                      
                                   """)

                await conn.execute("""CREATE TABLE IF NOT EXISTS 
                                          connector_utxo_state (name VARCHAR,
                                                                value BIGINT,
                                                                PRIMARY KEY(name));
                                   """)

                await conn.execute("""CREATE INDEX IF NOT EXISTS uutxo_out_tx_id
                                      ON connector_unconfirmed_utxo USING BTREE (out_tx_id);
                                   """)
                await conn.execute("""CREATE INDEX IF NOT EXISTS sutxo_out_tx_id
                                      ON connector_unconfirmed_stxo USING BTREE (out_tx_id);
                                   """)
                await conn.execute("""CREATE INDEX IF NOT EXISTS sutxo_tx_id
                                      ON connector_unconfirmed_stxo USING BTREE (tx_id);
                                   """)
                lb = await conn.fetchval("SELECT value FROM connector_utxo_state WHERE name='last_block';")
                lc = await conn.fetchval("SELECT value FROM connector_utxo_state WHERE name='last_cached_block';")
                if lb is None:
                    lb = -1
                    lc = 0
                    await conn.execute("INSERT INTO connector_utxo_state (name, value) VALUES ('last_block', 0);")
                    await conn.execute("INSERT INTO connector_utxo_state (name, value) VALUES ('last_cached_block', 0);")

                self.mempool_tx_count = await conn.fetchval("SELECT count(DISTINCT out_tx_id) "
                                                            "FROM connector_unconfirmed_utxo;")

        self.last_block_height = lb
        self.last_block_utxo_cached_height = lc
        if self.app_block_height_on_start:
            if self.app_block_height_on_start < self.last_block_height:
                self.log.critical("UTXO state last block %s app state last block %s " % (self.last_block_height,
                                                                                         self.app_block_height_on_start))
                raise Exception("App blockchain state behind connector blockchain state")
            if self.app_block_height_on_start < self.last_block_height:
                self.log.warning("Connector utxo height behind App height for %s blocks ..." %
                                 (self.app_block_height_on_start - self.last_block_height,))

        else:
            self.app_block_height_on_start = self.last_block_utxo_cached_height
        self.app_last_block = self.app_block_height_on_start
        if self.last_block_utxo_cached_height < self.app_block_height_on_start:
            self.last_block_utxo_cached_height = self.app_block_height_on_start


    async def zeromq_handler(self):
        while True:
            try:
                self.zmqContext = zmq.asyncio.Context()
                self.zmqSubSocket = self.zmqContext.socket(zmq.SUB)
                self.zmqSubSocket.setsockopt_string(zmq.SUBSCRIBE, "hashblock")
                if self.mempool_tx:
                    self.zmqSubSocket.setsockopt_string(zmq.SUBSCRIBE, "rawtx")
                self.zmqSubSocket.connect(self.zmq_url)
                self.log.info("Zeromq started")
                while True:
                    try:
                        msg = await self.zmqSubSocket.recv_multipart()
                        topic = msg[0]
                        body = msg[1]

                        if topic == b"hashblock":
                            self.last_zmq_msg = int(time.time())
                            if self.deep_synchronization:
                                continue
                            hash = body.hex()
                            if not self.get_next_block_mutex:
                                if self.active_block.done():
                                    self.log.warning("New block %s" % hash)
                                    self.get_next_block_mutex = True
                                    self.loop.create_task(self.get_next_block())

                        elif topic == b"rawtx":
                            self.last_zmq_msg = int(time.time())
                            if self.deep_synchronization or not self.mempool_tx:
                                continue
                            try:
                                tx = Transaction(body, format="raw")
                                self.new_tx[tx["txId"]] = (tx, int(time.time()))
                                if self.new_tx_handler is None or self.new_tx_handler.done():
                                    self.new_tx_handler = self.loop.create_task(self.handle_new_tx())
                            except:
                                self.log.critical("Transaction decode failed: %s" % body.hex())

                        if not self.active:
                            break
                    except asyncio.CancelledError:
                        self.log.warning("Zeromq handler terminating ...")
                        raise
                    except Exception as err:
                        self.log.error(str(err))

            except asyncio.CancelledError:
                self.zmqContext.destroy()
                self.log.warning("Zeromq handler terminated")
                break
            except Exception as err:
                self.log.error(str(err))
                await asyncio.sleep(1)
                self.log.warning("Zeromq handler reconnecting ...")
            if not self.active:
                self.log.warning("Zeromq handler terminated")
                break


    async def handle_new_tx(self):
        while self.new_tx and self.synchronized:
            if not self.block_txs_request.done():
                await self.block_txs_request
            h, v = self.new_tx.pop()
            self.new_tx_tasks += 1
            self.loop.create_task(self._new_transaction(v[0], v[1]))


    async def watchdog(self):
        """
        backup synchronization option
        in case zeromq failed
        """
        while True:
            try:
                while True:
                    await asyncio.sleep(20)
                    if self.mempool_tx:
                        if int(time.time()) - self.last_zmq_msg > 300 and self.zmqContext:
                            self.log.error("ZerroMQ no messages about 5 minutes")
                            try:
                                self.zeromq_task.cancel()
                                await asyncio.wait([self.zeromq_task])
                                self.zeromq_task(self.loop.create_task(self.zeromq_handler()))
                            except:
                                pass
                    try:
                        h = await self.rpc.getblockcount()
                        if self.node_last_block < h:
                            self.node_last_block = h
                            self.log.info("watchdog -> bitcoind node last block %s" % h)
                            if not self.get_next_block_mutex:
                                self.get_next_block_mutex = True
                                self.loop.create_task(self.get_next_block())
                    except:
                        pass
                    try:
                        if self.last_block_height > self.deep_sync_limit:
                            async with self.db_pool.acquire() as conn:
                                await conn.execute("DELETE FROM connector_block_state_checkpoint "
                                                   "WHERE height < $1;",
                                                   self.last_block_height - self.deep_sync_limit)
                        async with self.db_pool.acquire() as conn:
                            d = await conn.fetchval("SELECT n_dead_tup FROM pg_stat_user_tables "
                                                    "WHERE relname = 'connector_utxo' LIMIT 1;")
                            if d > 10000000:
                                self.log.info("Maintenance connector_utxo table ...")
                                t = time.time()
                                await conn.execute("VACUUM FULL connector_utxo;")
                                await conn.execute("ANALYZE connector_utxo;")
                                self.log.info("Maintenance connector_utxo table completed %s",
                                              round(time.time() - t, 2))
                    except:
                        pass


            except asyncio.CancelledError:
                self.log.info("connector watchdog terminated")
                break
            except Exception as err:
                self.log.error("watchdog error %s " % err)


    async def get_next_block(self):
        if self.active and self.active_block.done() and self.get_next_block_mutex:
            try:
                if self.node_last_block <= self.last_block_height + self.backlog:
                    d = await self.rpc.getblockcount()
                    if d == self.node_last_block:
                        if not self.synchronized:
                            self.log.debug("Blockchain is synchronized with backlog %s" % self.backlog)
                            self.synchronized = True
                        return
                    else:
                        self.node_last_block = d

                self.synchronized = False
                d = self.node_last_block - self.last_block_height

                if d > self.deep_sync_limit:
                    if not self.deep_synchronization:
                        self.log.info("Deep synchronization mode")
                else:
                    if self.deep_synchronization:
                        self.log.info("Switch from deep synchronization mode")
                        if self.utxo_data:
                            await self.uutxo.flush_mempool()
                        if self.flush_app_caches_handler:
                            await self.flush_app_caches_handler(self.last_block_height)
                        # clear preload caches
                        if self.utxo_data and len(self.sync_utxo.cache):
                            self.log.info("Flush utxo cache ...")
                            while self.app_last_block < self.last_block_height:
                                self.log.debug("Waiting app ... Last block %s; "
                                               "App last block %s;" % (self.last_block_height, self.app_last_block))
                                await asyncio.sleep(5)

                            self.log.info("Last block %s App last block %s" % (self.last_block_height,
                                                                               self.app_last_block))
                            print(self.sync_utxo.checkpoints )
                            self.sync_utxo.checkpoints =  deque([self.last_block_height])

                            self.sync_utxo.size_limit = 0
                            while  self.sync_utxo.save_process or self.sync_utxo.cache or self.sync_utxo.pending_saved:
                                self.log.info("wait for utxo cache flush [%s/%s]..." % (len(self.sync_utxo.cache),
                                                                                        len(self.sync_utxo.pending_saved)) )
                                self.sync_utxo.create_checkpoint(self.last_block_height, self.app_last_block)
                                await self.sync_utxo.commit()

                                await asyncio.sleep(10)


                            self.log.info("Flush utxo cache completed %s %s " % (len(self.sync_utxo.cache),
                                                                                   len(self.sync_utxo.pending_saved),
                                                                                   ))

                        if self.synchronization_completed_handler:
                            try:
                                [self.block_loader.worker[i].terminate() for i in self.block_loader.worker]
                            except:
                                pass
                            await self.synchronization_completed_handler()
                        self.deep_synchronization = False
                        self.deep_sync_limit = self.node_last_block
                        self.total_received_tx = 0
                        self.total_received_tx_time = 0
                block = None
                if self.deep_synchronization:
                    raw_block = self.block_preload.pop(self.last_block_height + 1)
                    if raw_block:
                        q = time.time()
                        block = loads(raw_block)
                        self.blocks_decode_time += time.time() - q
                    else:
                        self.loop.create_task(self.retry())
                        return
                if not block:
                    h = await self.rpc.getblockhash(self.last_block_height + 1)
                    block = await self._get_block_by_hash(h)
                    block["checkpoint"] = self.last_block_height + 1
                    block["height"] = self.last_block_height + 1
                    if self.deep_synchronization:
                        coinbase = block["rawTx"][0]["vIn"][0]["scriptSig"]
                        block["miner"] = None
                        for tag in MINER_COINBASE_TAG:
                            if coinbase.find(tag) != -1:
                                block["miner"] = json.dumps(MINER_COINBASE_TAG[tag])
                                break
                        else:
                            try:
                                address_hash = block["rawTx"][0]["vOut"][0]["addressHash"]
                                script_hash = False if block["rawTx"][0]["vOut"][0]["nType"] == 1 else True
                                a = hash_to_address(address_hash, script_hash=script_hash)
                                if a in MINER_PAYOUT_TAG:
                                    block["miner"] = json.dumps(MINER_PAYOUT_TAG[a])
                            except:
                                pass

                        if self.option_tx_map:
                            block["txMap"], block["stxo"] = deque(), deque()

                        if self.option_merkle_proof:
                            mt = merkle_tree(block["rawTx"][i]["txId"] for i in block["rawTx"])
                        if self.option_analytica:
                            block["stat"] = {
                                "oCountTotal": 0,
                                "oAmountMinPointer": 0,
                                "oAmountMinValue": 0,
                                "oAmountMaxPointer": 0,
                                "oAmountMaxValue": 0,
                                "oAmountTotal": 0,
                                "oAmountMapCount": dict(),
                                "oAmountMapAmount": dict(),
                                "oTypeMapCount": dict(),
                                "oTypeMapAmount": dict(),
                                "oTypeMapSize": dict(),

                                "iCountTotal": 0,
                                "iAmountMinPointer": 0,
                                "iAmountMinValue": 0,
                                "iAmountMaxPointer": 0,
                                "iAmountMaxValue": 0,
                                "iAmountTotal": 0,
                                "iAmountMapCount": dict(),
                                "iAmountMapAmount": dict(),
                                "iTypeMapCount": dict(),
                                "iTypeMapAmount": dict(),
                                "iP2SHtypeMapCount": dict(),
                                "iP2SHtypeMapAmount": dict(),
                                "iP2WSHtypeMapCount": dict(),
                                "iP2WSHtypeMapAmount": dict(),

                                "txCountTotal": 0,
                                "txAmountMinPointer": 0,
                                "txAmountMinValue": 0,
                                "txAmountMaxPointer": 0,
                                "txAmountMaxValue": 0,
                                "txAmountMapCount": dict(),
                                "txAmountMapAmount": dict(),
                                "txAmountMapSize": dict(),
                                "txAmountTotal": 0,
                                "txSizeMinPointer": 0,
                                "txSizeMinValue": 0,
                                "txSizeMaxPointer": 0,
                                "txSizeMaxValue": 0,
                                "txSizeTotal": 0,
                                "txBSizeTotal": 0,
                                "txVSizeTotal": 0,
                                "txSizeMapCount": dict(),
                                "txSizeMapAmount": dict(),
                                "txTypeMapCount": dict(),
                                "txTypeMapSize": dict(),
                                "txTypeMapAmount": dict(),
                                "txFeeMinPointer": 0,
                                "txFeeMinValue": 0,
                                "txFeeMaxPointer": 0,
                                "txFeeMaxValue": 0,
                                "txFeeTotal": 0,
                                "txFeeRateMinPointer": 0,
                                "txFeeRateMinValue": 0,
                                "txFeeRateMaxPointer": 0,
                                "txFeeRateMaxValue": 0,
                                "txFeeRateTotal": 0,
                                "txFeeRateMapCount": dict(),
                                "txFeeRateMapAmount": dict(),
                                "txFeeRateMapSize": dict(),
                                "txVFeeRateMinPointer": 0,
                                "txVFeeRateMinValue": 0,
                                "txVFeeRateMaxPointer": 0,
                                "txVFeeRateMaxValue": 0,
                                "txVFeeRateTotal": 0,
                                "txVFeeRateMapCount": dict(),
                                "txVFeeRateMapAmount": dict(),
                                "txVFeeRateMapSize": dict()
                            }

                        for t in block["rawTx"]:
                            tx = block["rawTx"][t]

                            if self.option_merkle_proof:
                                block["rawTx"][t]["merkleProof"] = b''.join(merkle_proof(mt, t, return_hex=False))
                            if self.option_analytica:
                                bip69, rbf = True, False
                                hp, op = None, None
                                block["rawTx"][t]["inputsAmount"] = 0

                            # # get inputs
                            for i in tx["vOut"]:
                                out = tx["vOut"][i]
                                if out["nType"] not in (8, 3):
                                    if "addressHash" not in out:
                                        address = b"".join((bytes([out["nType"]]), out["scriptPubKey"]))
                                    else:
                                        address = b"".join((bytes([out["nType"]]), out["addressHash"]))
                                    if self.option_tx_map:
                                        block["txMap"].append(((block["height"]<<39)+(t<<20)+(1<<19)+i,
                                                               address, out["value"]))
                                    block["rawTx"][t]["vOut"][i]["_address"] = address

                                    if self.option_analytica:
                                        pointer = (block["height"]<<39)+(t<<20)+(1<<19)+i
                                        amount = block["rawTx"][t]["vOut"][i]["value"]
                                        block["stat"]["oCountTotal"] += 1
                                        block["stat"]["oAmountTotal"] += amount
                                        if block["stat"]["oAmountMinPointer"] == 0 or \
                                                block["stat"]["oAmountMinValue"] > amount:
                                            block["stat"]["oAmountMinPointer"] = pointer
                                            block["stat"]["oAmountMinPointer"] = amount
                                        if block["stat"]["oAmountMaxValue"] < amount:
                                            block["stat"]["oAmountMaxPointer"] = pointer
                                            block["stat"]["oAmountMaxValue"] = amount
                                        amount_key = str(floor(log10(amount))) if amount else "null"
                                        try:
                                            block["stat"]["oAmountMapCount"][amount_key] += 1
                                        except:
                                            block["stat"]["oAmountMapCount"][amount_key] = 1
                                        try:
                                            block["stat"]["oAmountMapAmount"][amount_key] += amount
                                        except:
                                            block["stat"]["oAmountMapAmount"][amount_key] = amount

                            if not block["rawTx"][t]["coinbase"]:
                                for i in block["rawTx"][t]["vIn"]:
                                    inp = block["rawTx"][t]["vIn"][i]
                                    outpoint = b"".join((inp["txId"], int_to_bytes(inp["vOut"])))
                                    block["rawTx"][t]["vIn"][i]["_outpoint"] = outpoint

                                    if self.option_analytica:
                                        if not rbf and inp["sequence"] < 0xfffffffe:  rbf = True
                                        if bip69:
                                            h = rh2s(inp["txId"])
                                            if hp is not None:
                                                if hp > h:
                                                    bip69 = False
                                                elif hp == h and op > inp["vOut"]:
                                                    bip69 = False
                                            hp, op = h, inp["vOut"]

                            if self.option_analytica:
                                tx = block["rawTx"][t]
                                pointer = (block["height"] << 19) + t
                                amount, size = tx["amount"], tx["size"]
                                amount_key = str(floor(log10(amount))) if amount else "null"
                                block["stat"]["txCountTotal"] += 1
                                if block["stat"]["txAmountMinPointer"] == 0 or \
                                        block["stat"]["txAmountMinValue"] > amount:
                                    block["stat"]["txAmountMinPointer"] = pointer
                                    block["stat"]["txAmountMinValue"] = amount

                                if block["stat"]["txAmountMaxValue"] < amount:
                                    block["stat"]["txAmountMaxPointer"] = pointer
                                    block["stat"]["txAmountMaxValue"] = amount

                                try:
                                    block["stat"]["txAmountMapAmount"][amount_key] += amount
                                except:
                                    block["stat"]["txAmountMapAmount"][amount_key] = amount
                                try:
                                    block["stat"]["txAmountMapCount"][amount_key] += 1
                                except:
                                    block["stat"]["txAmountMapCount"][amount_key] = 1

                                try:
                                    block["stat"]["txAmountMapSize"][amount_key] += size
                                except:
                                    block["stat"]["txAmountMapSize"][amount_key] = size


                self.loop.create_task(self._new_block(block))

            except Exception as err:
                self.log.error("get next block failed %s" % str(err))
            finally:
                self.get_next_block_mutex = False


    async def retry(self):
        await asyncio.sleep(1)
        self.get_next_block_mutex = True
        self.loop.create_task(self.get_next_block())


    async def _get_block_by_hash(self, hash):
        try:
            if self.deep_synchronization:
                q = time.time()
                self.non_cached_blocks += 1
                raw_block = await self.rpc.getblock(hash, 0)
                self.blocks_download_time += time.time() - q
                q = time.time()
                block = decode_block_tx(raw_block)
                self.blocks_decode_time += time.time() - q
            else:
                q = time.time()
                block = await self.rpc.getblock(hash)
                self.blocks_download_time += time.time() - q
            header = await self.rpc.getblockheader(hash, False)
            block["header"] = bytes_from_hex(header)
            return block
        except Exception:
            self.log.error("get block by hash %s FAILED" % hash)


    async def _new_block(self, block):
        if not self.active: return
        tq = time.time()
        if self.deep_synchronization:  block["height"] = self.last_block_height + 1
        if self.last_block_height >= block["height"]:  return
        if not self.active_block.done():  return
        try:
            self.active_block = asyncio.Future()

            if self.deep_synchronization:
                if self.last_block_height < self.last_block_utxo_cached_height:
                    if not self.cache_loading:
                        self.log.info("Bootstrap UTXO cache ...")
                    self.cache_loading = True
                else:
                    if self.cache_loading and self.deep_synchronization:
                        self.log.info("UTXO Cache bootstrap completed")
                    self.cache_loading = False
            else:
                if self.block_headers_cache.get(block["hash"]) is not None:
                    return

            if not self.deep_synchronization:
                mount_point_exist = await self.verify_block_position(block)
                if not mount_point_exist:
                    return

            if self.deep_synchronization:
                await self._block_as_transactions_batch(block)
                if not self.cache_loading or block["height"] > self.app_block_height_on_start:
                    if self.block_batch_handler:
                        t = time.time()
                        await self.block_batch_handler(block)
                        self.batch_handler += time.time() - t
                if self.total_received_tx - self.total_received_tx_stat > 100000:
                    self.report_sync_process(block["height"])
                    if self.utxo_data:
                        if self.sync_utxo.len() > self.sync_utxo.size_limit:
                            if not self.sync_utxo.save_process:
                                if self.sync_utxo.checkpoints and not self.cache_loading:
                                    if self.sync_utxo.checkpoints[0] < block["height"]:
                                        self.sync_utxo.create_checkpoint(block["height"], self.app_last_block)
                                        if self.sync_utxo.save_process:
                                            self.loop.create_task(self.sync_utxo.commit())

            else:
                # call before block handler
                if self.before_block_handler:
                    await self.before_block_handler(block)

                await self.fetch_block_transactions(block)

                if self.utxo_data:
                    if self.db_type == "postgresql":
                        async with self.db_pool.acquire() as conn:
                            async with conn.transaction():
                                data = await  self.uutxo.apply_block_changes([s2rh(h) for h in block["tx"]],
                                                                             block["height"], conn)
                                block["mempoolInvalid"] = {"tx": data["invalid_txs"],
                                                           "inputs": data["dbs_stxo"],
                                                           "outputs": data["dbs_uutxo"]}
                                if self.block_handler:
                                    await self.block_handler(block, conn)
                                await conn.execute("UPDATE connector_utxo_state SET value = $1 "
                                                   "WHERE name = 'last_block';", block["height"])
                                await conn.execute("UPDATE connector_utxo_state SET value = $1 "
                                                   "WHERE name = 'last_cached_block';", block["height"])


                elif self.block_handler:
                    await self.block_handler(block, None)
            self.block_headers_cache.set(block["hash"], block["height"])
            self.last_block_height = block["height"]
            self.app_last_block = block["height"]
            self.blocks_processed_count += 1

            # after block added handler
            if self.after_block_handler:
                if not self.cache_loading or block["height"] > self.app_block_height_on_start:
                    try:
                        await self.after_block_handler(block)
                    except:
                        pass
            if not self.deep_synchronization:
                if self.mempool_tx:
                    self.mempool_tx_count -= len(block["tx"]) + len(block["mempoolInvalid"]["tx"])
                    self.log.debug("Mempool transactions %s; "
                                   "orphaned transactions: %s; "
                                   "resolved orphans %s" % (self.mempool_tx_count,
                                                            len(self.tx_orphan_buffer),
                                                            self.tx_orphan_resolved))
                self.log.info("Block %s -> %s; tx count %s;" % (block["height"], block["hash"],len(block["tx"])))

        except Exception as err:
            if self.await_tx:
                self.await_tx = set()
            for i in self.await_tx_future:
                if not self.await_tx_future[i].done():
                    self.await_tx_future[i].cancel()
            self.await_tx_future = dict()
            self.log.error("block %s error %s" % (block["height"], str(err)))
            print(traceback.format_exc())


        finally:
            if self.node_last_block > self.last_block_height:
                self.get_next_block_mutex = True

                self.loop.create_task(self.get_next_block())
            else:
                self.synchronized = True

            self.blocks_processing_time += time.time() - tq
            self.active_block.set_result(True)


    async def verify_block_position(self, block):
        try:
            previousblockhash = block["previousBlockHash"]
            block["previousblockhash"] = previousblockhash
        except:
            try:
                previousblockhash = block["previousblockhash"]
                block["previousBlockHash"] = previousblockhash
            except:
                return

        if self.block_headers_cache.len() == 0:
            if self.chain_tail_start_len and self.last_block_height:
                self.log.critical("Connector error! Node out of sync "
                                  "no parent block in chain tail %s" % block["previousblockhash"])
                await asyncio.sleep(30)
                raise Exception("Node out of sync")
            else:
                return True

        if self.block_headers_cache.get_last_key() != block["previousblockhash"]:
            if self.orphan_handler:
                if self.utxo_data:
                    if self.db_type == "postgresql":
                        async with self.db_pool.acquire() as conn:
                            async with conn.transaction():
                                data = await self.uutxo.rollback_block(conn)
                                if self.mempool_tx:
                                    self.mempool_tx_count += data["block_tx_count"] + len(data["mempool"]["tx"])
                                await self.orphan_handler(data, conn)
                                await conn.execute("UPDATE connector_utxo_state SET value = $1 "
                                                   "WHERE name = 'last_block';",
                                                   self.last_block_height - 1)
                                await conn.execute("UPDATE connector_utxo_state SET value = $1 "
                                                   "WHERE name = 'last_cached_block';",
                                                   self.last_block_height - 1)
                            self.mempool_tx_count = await conn.fetchval("SELECT count(DISTINCT out_tx_id) "
                                                                        "FROM connector_unconfirmed_utxo;")
                            self.log.debug("Mempool transactions %s; "
                                           "orphaned transactions: %s; "
                                           "resolved orphans %s" % (self.mempool_tx_count,
                                                                    len(self.tx_orphan_buffer),
                                                                    self.tx_orphan_resolved))


                else:
                    await self.orphan_handler(self.last_block_height, None)
            b_hash, _ = self.block_headers_cache.pop_last()

            self.last_block_height -= 1
            self.app_last_block -= 1
            self.log.warning("Removed orphaned block %s %s" % (self.last_block_height + 1, b_hash))
            return False
        return True


    async def _block_as_transactions_batch(self, block):
        t, t2 = time.time(), 0
        height = block["height"]
        if self.option_tx_map:
            tx_map_append = block["txMap"].append
        if self.utxo_data:
            #
            #  utxo mode
            #  fetch information about destroyed coins
            #  save new coins to utxo table
            #
            if self.option_block_filters:
                M = self.option_block_filter_fps
                N = block["_N"]

            for q in block["rawTx"]:
                tx = block["rawTx"][q]
                for i in tx["vOut"]:
                    if "_s_" in tx["vOut"][i]:
                        self.coins += 1
                    else:
                        out = tx["vOut"][i]
                        if self.skip_opreturn and out["nType"] in (3, 8):
                            self.op_return += 1
                            continue
                        self.coins += 1
                        self.sync_utxo.set(b"".join((tx["txId"], int_to_bytes(i))),
                                           (height << 39) + (q << 20) + (1 << 19) + i,
                                           out["value"],
                                           out["_address"])

            stxo, missed = dict(), deque()
            for q in block["rawTx"]:
                tx = block["rawTx"][q]
                if not tx["coinbase"]:
                    if self.sync_utxo:
                        for i in tx["vIn"]:
                            self.destroyed_coins += 1
                            inp = tx["vIn"][i]
                            try:
                                # preloaded and destroyed in preload batch
                                tx["vIn"][i]["coin"] = inp["_a_"]
                                self.preload_cached_annihilated += 1
                                self.preload_cached_total += 1
                            except:
                                try:
                                    # coin was loaded from db on preload stage
                                    tx["vIn"][i]["coin"] = inp["_l_"]
                                    self.preload_cached_total += 1
                                    self.preload_cached += 1
                                    self.sync_utxo.scheduled_to_delete.append(inp["_outpoint"])
                                except:
                                    r = self.sync_utxo.get(inp["_outpoint"])
                                    if r:
                                        tx["vIn"][i]["coin"] = r
                                    else:
                                        missed.append((inp["_outpoint"], (height<<39)+(q<<20)+i, q, i))

            if missed:
                t2 = time.time()
                await self.sync_utxo.load_utxo()
                t2 =time.time() - t2
                self.batch_load_utxo += t2
                if  self.cache_loading:
                    if height > self.app_block_height_on_start:
                        await self.sync_utxo.load_utxo_from_daemon()
                for o, s, q, i in missed:
                    block["rawTx"][q]["vIn"][i]["coin"] = self.sync_utxo.get_loaded(o)
                    if  block["rawTx"][q]["vIn"][i]["coin"] is None:
                        if self.cache_loading:
                            if height > self.app_block_height_on_start:
                                raise Exception("utxo get failed ")
                        else:
                            raise Exception("utxo get failed %s" % rh2s(block["rawTx"][q]["vIn"][i]["txId"]))
                    if height > self.app_block_height_on_start:
                        if self.option_tx_map:
                            tx_map_append(((height << 39)+(q<<20)+i,
                                           block["rawTx"][q]["vIn"][i]["coin"][2],
                                           block["rawTx"][q]["vIn"][i]["coin"][1]))
                            block["stxo"].append((block["rawTx"][q]["vIn"][i]["coin"][0],
                                                 (height << 39)+(q<<20)+i))
                            if self.option_block_filters:
                                h = map_into_range(siphash(block["rawTx"][q]["vIn"][i]["coin"][2],
                                                           v_0=block["_v_0"],
                                                           v_1=block["_v_0"]), N * M)
                                block["filter"].append(h)

                        if self.option_analytica:
                            r = block["rawTx"][q]["vIn"][i]["coin"]
                            amount = r[1]
                            block["rawTx"][q]["inputsAmount"] += amount
                            pointer = (height << 39) + (q << 20) + (0 << 19) + i
                            type = r[2][0]
                            block["stat"]["iCountTotal"] += 1
                            block["stat"]["iAmountTotal"] += amount
                            if block["stat"]["iAmountMinPointer"] == 0 or \
                                    block["stat"]["iAmountMinValue"] > amount:
                                block["stat"]["iAmountMinPointer"] = pointer
                                block["stat"]["iAmountMinValue"] = amount
                            if block["stat"]["iAmountMaxValue"] < amount:
                                block["stat"]["iAmountMaxPointer"] = pointer
                                block["stat"]["iAmountMaxValue"] = amount
                            amount_key = str(floor(log10(amount))) if amount else "null"
                            try:
                                block["stat"]["iAmountMapCount"][amount_key] += 1
                            except:
                                block["stat"]["iAmountMapCount"][amount_key] = 1
                            try:
                                block["stat"]["iAmountMapAmount"][amount_key] += amount
                            except:
                                block["stat"]["iAmountMapAmount"][amount_key] = amount
                            try:
                                block["stat"]["iTypeMapCount"][type] += 1
                            except:
                                block["stat"]["iTypeMapCount"][type] = 1
                            try:
                                block["stat"]["iTypeMapAmount"][type] += amount
                            except:
                                block["stat"]["iTypeMapAmount"][type] = amount

                            if type == 1 or type == 6:
                                s = parse_script(r[2][1:])
                                st = s["type"]
                                if st == "MULTISIG":
                                    st += "_%s/%s" % (s["reqSigs"], s["pubKeys"])
                                    if type == 1:
                                        try:
                                            block["stat"]["iP2SHtypeMapCount"][st] += 1
                                        except:
                                            block["stat"]["iP2SHtypeMapCount"][st] = 1
                                        try:
                                            block["stat"]["iP2SHtypeMapAmount"][st] += amount
                                        except:
                                            block["stat"]["iP2SHtypeMapAmount"][st] = amount
                                    else:
                                        try:
                                            block["stat"]["iP2WSHtypeMapCount"][st] += 1
                                        except:
                                            block["stat"]["iP2WSHtypeMapCount"][st] = 1
                                        try:
                                            block["stat"]["iP2WSHtypeMapAmount"][st] += amount
                                        except:
                                            block["stat"]["iP2WSHtypeMapAmount"][st] = amount

            if self.option_block_filters:
                print(block["height"], len(block["filter"]), block["_N"], N, block["_I"])
                assert len(block["filter"]) == N
                block["filter"] = create_gcs(block["filter"], hashed=True,
                                             M=M, P=self.option_block_filter_bits ,hex=0)

        self.total_received_tx += len(block["rawTx"])
        self.total_received_tx_last += len(block["rawTx"])
        self.batch_parsing += (time.time() - t) - t2


    def report_sync_process(self, height):
        batch_tx_count = self.total_received_tx - self.total_received_tx_stat
        tx_rate = round(self.total_received_tx / (time.time() - self.start_time), 2)
        io_rate = round((self.coins + self.destroyed_coins) / (time.time() - self.start_time), 2)
        tx_rate_last = round(self.total_received_tx_last / (time.time() - self.start_time_last), 2)
        self.total_received_tx_last = 0
        self.start_time_last = time.time()
        self.total_received_tx_stat = self.total_received_tx

        self.log.info("Blocks %s; tx/s rate: %s; "
                      "io/s rate %s; Uptime %s" % (height,
                                                   tx_rate,
                                                   io_rate,
                                                   seconds_to_age(int(time.time() - self.start_time))))
        if self.utxo_data:
            loading = "Loading UTXO cache mode ... " if self.cache_loading else ""

            # last batch stat
            self.log.debug("- Batch ---------------")
            self.log.debug("    Rate tx/s %s; transactions count %s" % (tx_rate_last, batch_tx_count))
            self.log.debug("    Load utxo time %s; parsing time %s" % (round(self.batch_load_utxo, 2),
                                                                       round(self.batch_parsing, 2)))
            self.log.debug("    Batch time %s; "
                           "Batch handler time %s;" % (round(time.time() - self.batch_time, 2),
                                                       round(self.batch_handler, 2)))
            self.batch_handler = 0
            self.batch_load_utxo = 0
            self.batch_parsing = 0
            self.batch_time = time.time()

            # blocks stat
            self.log.debug("- Blocks --------------")
            self.log.debug("    Not cached count %s; "
                           "cached count %s; "
                           "cache size %s M;" % (self.non_cached_blocks,
                                                 self.block_preload.len(),
                                                 round(self.block_preload._store_size / 1024 / 1024, 2)))
            if self.block_preload._store:
                self.log.debug("    Cache first block %s; "
                               "cache last block %s;" % (next(iter(self.block_preload._store)),
                                                         next(reversed(self.block_preload._store))))
            self.log.debug("    Preload coins cache -> %s:%s [%s] "
                           "preload cache efficiency %s;" % (self.preload_cached,
                                                             self.preload_cached_annihilated,
                                                             self.preload_cached_total,
                                                             round(self.preload_cached_total
                                                                   / self.destroyed_coins, 4)))

            # utxo stat
            self.log.debug("- UTXO ----------------")
            if loading: self.log.debug(loading)

            self.log.debug("    Cache count %s; hit rate: %s;" % (self.sync_utxo.len(),
                                                                  round(self.sync_utxo.hit_rate(), 4)))
            self.log.debug("    Checkpoint block %s; App checkpoint %s" % (self.sync_utxo.checkpoint,
                                                                           self.app_last_block))
            self.log.debug("    Saved to db %s; deleted from db %s; "
                           "loaded  from db %s" % (self.sync_utxo.saved_utxo_count,
                                                   self.sync_utxo.deleted_utxo_count,
                                                   self.sync_utxo.loaded_utxo_count))
            if self.sync_utxo.read_from_db_batch_time:
                c = round(self.sync_utxo.read_from_db_count / self.sync_utxo.read_from_db_batch_time, 4)
            else:
                c = 0
            self.log.debug("    Read from db last batch %s; "
                           "count %s; "
                           "batch time %s; "
                           "rate %s; "
                           "total time %s; " % (round(self.sync_utxo.read_from_db_time, 4),
                                                self.sync_utxo.read_from_db_count,
                                                round(self.sync_utxo.read_from_db_batch_time, 4),
                                                c,
                                                int(self.sync_utxo.read_from_db_time_total)))
            self.sync_utxo.read_from_db_batch_time = 0
            self.sync_utxo.read_from_db_time = 0
            self.sync_utxo.read_from_db_count = 0

            # coins stat
            self.log.debug("- Coins ---------------")
            self.log.debug("    Coins %s; destroyed %s; "
                           "unspent %s; op_return %s;" % (self.coins,
                                                          self.destroyed_coins,
                                                          self.coins - self.destroyed_coins,
                                                          self.op_return))
            self.log.debug("    Coins destroyed in cache %s; "
                           "cache efficiency  %s [%s];" % (self.sync_utxo._hit,
                                                           round(self.sync_utxo._hit / self.destroyed_coins, 4),
                                                           round((self.sync_utxo._hit + self.preload_cached_annihilated)
                                                                 / self.destroyed_coins, 4)))
            self.log.debug("---------------------")


    async def fetch_block_transactions(self, block):
        q = time.time()
        missed = set()
        tx_count = len(block["tx"])

        self.block_txs_request = asyncio.Future()
        self.log.debug("Wait unconfirmed tx tasks  %s" % len(self.tx_in_process))
        if not self.unconfirmed_tx_processing.done():
            await self.unconfirmed_tx_processing

        for h in block["tx"]:
            try:
                self.tx_cache[h]
            except:
                missed.add(h)



        if self.utxo_data:
            if self.db_type == "postgresql":
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch("SELECT distinct tx_id FROM  connector_unconfirmed_stxo "
                                            "WHERE tx_id = ANY($1);", set(s2rh(t) for t in missed))

                    for row in rows:
                        missed.remove(rh2s(row["tx_id"]))
                    if missed:
                        coinbase = await conn.fetchval("SELECT   out_tx_id FROM connector_unconfirmed_utxo "
                                                  "WHERE out_tx_id  = $1 LIMIT 1;", s2rh(block["tx"][0]))
                        if coinbase:
                            if block["tx"][0] in missed:
                                missed.remove(block["tx"][0])

        self.log.debug("Block missed transactions  %s from %s" % (len(missed), tx_count))

        if missed:
            self.missed_tx = set(missed)
            self.await_tx = set(missed)
            self.await_tx_future = {s2rh(i): asyncio.Future() for i in missed}
            self.block_timestamp = block["time"]
            self.loop.create_task(self._get_missed())
            try:
                await asyncio.wait_for(self.block_txs_request, timeout=self.block_timeout)
            except asyncio.CancelledError:
                # refresh rpc connection session
                try:
                    await self.rpc.close()
                    self.rpc = aiojsonrpc.rpc(self.rpc_url, self.loop, timeout=self.rpc_timeout)
                except:
                    pass
                raise RuntimeError("block transaction request timeout")
        else:
            self.block_txs_request.set_result(True)

        self.total_received_tx += tx_count
        self.total_received_tx_last += tx_count
        self.total_received_tx_time += time.time() - q
        rate = round(self.total_received_tx/self.total_received_tx_time)
        self.log.debug("Transactions received: %s [%s] received tx rate tx/s ->> %s <<" % (tx_count, time.time() - q, rate))


    async def _get_transaction(self, tx_hash):
        try:
            raw_tx = await self.rpc.getrawtransaction(tx_hash)
            tx = Transaction(raw_tx, format="raw")
            self.new_tx[tx["txId"]] = (tx, int(time.time()))
        except Exception as err:
            self.log.error("get transaction failed: %s" % str(err))


    async def _get_missed(self):
        if self.get_missed_tx_threads <= self.get_missed_tx_threads_limit:
            self.get_missed_tx_threads += 1
            # start more threads
            if len(self.missed_tx) > 1:
                self.loop.create_task(self._get_missed())
            while True:
                if not self.missed_tx: break
                try:
                    batch = list()
                    while self.missed_tx:
                        h = self.missed_tx.pop()
                        batch.append(["getrawtransaction", h])
                        if len(batch) >= self.rpc_batch_limit:
                            break
                    result = await self.rpc.batch(batch)
                    for r in result:
                        try:
                            tx = Transaction(r["result"], format="raw")
                        except:
                            self.log.error("Transaction decode failed: %s" % r["result"])
                            raise Exception("Transaction decode failed")
                        self.loop.create_task(self._new_transaction(tx, self.block_timestamp, True))
                except Exception as err:
                    self.log.error("_get_missed exception %s " % str(err))
                    self.await_tx = set()
                    self.block_txs_request.cancel()
            self.get_missed_tx_threads -= 1


    async def wait_block_dependences(self, tx):
        while self.await_tx_future:
            for i in tx["vIn"]:
                if tx["vIn"][i]["txId"] in self.await_tx_future:
                    if not self.await_tx_future[tx["vIn"][i]["txId"]].done():
                        await self.await_tx_future[tx["vIn"][i]["txId"]]
                        break
            else:
                break


    async def _new_transaction(self, tx, timestamp, block_tx = False):
        tx_hash = rh2s(tx["txId"])
        if tx_hash in self.tx_in_process:
            if not block_tx:
                self.new_tx_tasks -= 1
            return

        if self.tx_cache.has_key(tx_hash):
            self.new_tx_tasks -= 1
            return

        try:
            self.tx_in_process.add(tx_hash)
            if block_tx:
                if not tx["coinbase"]:
                    await self.wait_block_dependences(tx)
            else:
                if self.unconfirmed_tx_processing.done():
                    self.unconfirmed_tx_processing = asyncio.Future()

            if self.utxo_data:
                tx["double_spent"] = False
                commit_uutxo_buffer = set()
                commit_ustxo_buffer = set()
                if not tx["coinbase"]:
                    for i in tx["vIn"]:
                        self.destroyed_coins += 1
                        tx["vIn"][i]["outpoint"] = b"".join((tx["vIn"][i]["txId"], int_to_bytes(tx["vIn"][i]["vOut"])))
                        self.uutxo.load_buffer.append(tx["vIn"][i]["outpoint"])

                    await self.uutxo.load_utxo_data()

                    for i in tx["vIn"]:
                        tx["vIn"][i]["coin"] = self.uutxo.loaded_utxo[tx["vIn"][i]["outpoint"]]
                        commit_ustxo_buffer.add((tx["vIn"][i]["outpoint"], 0,
                                                 tx["vIn"][i]["txId"], tx["txId"], i, tx["vIn"][i]["coin"][2]))
                        try:
                            tx["vIn"][i]["double_spent"] = self.uutxo.loaded_ustxo[tx["vIn"][i]["outpoint"]]
                            tx["double_spent"] = True
                        except:
                            pass

                for i in tx["vOut"]:
                    try:
                        address = b"".join((bytes([tx["vOut"][i]["nType"]]), tx["vOut"][i]["addressHash"]))
                    except:
                        address = b"".join((bytes([tx["vOut"][i]["nType"]]), tx["vOut"][i]["scriptPubKey"]))

                    commit_uutxo_buffer.add((b"".join((tx["txId"],int_to_bytes(i))),
                                             tx["txId"],
                                             address,
                                             tx["vOut"][i]["value"]))
                async with self.db_pool.acquire() as conn:
                    async with conn.transaction():
                        await self.uutxo.commit_tx(commit_uutxo_buffer, commit_ustxo_buffer, conn)

                        if self.tx_handler:
                            await self.tx_handler(tx, timestamp, conn)
            else:
                if self.tx_handler:
                    await self.tx_handler(tx, timestamp, None)

            self.tx_cache[tx_hash] = True

            if block_tx:
                self.await_tx.remove(tx_hash)
                self.await_tx_future[tx["txId"]].set_result(True)
                # self.log.debug("tx %s; left %s" % (tx_hash, len(self.await_tx)))


            # in case recently added transaction
            # in dependency list for orphaned transactions
            # try add orphaned again
            if tx_hash in self.tx_orphan_buffer:
                rows = self.tx_orphan_buffer.delete(tx_hash)
                self.tx_orphan_resolved += 1
                for row in rows:
                    self.new_tx[tx["txId"]] = (row, int(time.time()))
            self.mempool_tx_count += 1

        except asyncio.CancelledError:
            pass

        except KeyError as err:
            # transaction orphaned
            try:
                self.tx_orphan_buffer[rh2s(err.args[0][:32])].append(tx)
            except:
                self.tx_orphan_buffer[rh2s(err.args[0][:32])] = [tx]
            # self.log.debug("tx orphaned %s" % tx_hash)
            self.loop.create_task(self._get_transaction(rh2s(err.args[0][:32])))
            # self.log.debug("requested %s" % rh2s(err.args[0][:32]))
            # clear orphaned transactions buffer over limit
            while len(self.tx_orphan_buffer) > self.tx_orphan_buffer_limit:
                key, value = self.tx_orphan_buffer.pop()

        except Exception as err:
            try:
                # check if transaction already exist
                if err.detail.find("already exists") != -1:
                    if block_tx:
                        self.await_tx.remove(tx_hash)
                        self.await_tx_future[tx["txId"]].set_result(True)
                return
            except:
                pass

            if block_tx:
                self.log.critical("new transaction error %s" % err)
                print(traceback.format_exc())
                self.await_tx = set()
                self.block_txs_request.cancel()
                for i in self.await_tx_future:
                    if not self.await_tx_future[i].done():
                        self.await_tx_future[i].cancel()
            self.log.critical("failed tx - %s [%s]" % (tx_hash, str(err)))

        finally:
            self.tx_in_process.remove(tx_hash)

            if block_tx:
                if not self.block_txs_request.done():
                    if not self.await_tx:
                        self.block_txs_request.set_result(True)
                        self.log.debug("Block transactions request completed ")
            else:
                self.new_tx_tasks -= 1
                if self.new_tx_tasks < 1 and not self.tx_in_process:
                    if not self.unconfirmed_tx_processing.done():
                        self.unconfirmed_tx_processing.set_result(True)


    async def stop(self):
        self.active = False
        self.log.warning("New block processing restricted")
        self.log.warning("Stopping node connector ...")
        try:
            for i in self.block_loader.worker:
                self.block_loader.worker[i].terminate()
        except:
            pass
        [task.cancel() for task in self.tasks]
        if self.tasks:
            await asyncio.wait(self.tasks)
        try:
            self.zeromq_task.cancel()
            await asyncio.wait([self.zeromq_task])
        except:
            pass
        if not self.active_block.done():
            self.log.warning("Waiting active block task ...")
            await self.active_block
        if self.rpc: await self.rpc.close()
        if self.zmqContext:
            self.zmqContext.destroy()
        self.log.warning('Node connector terminated')




