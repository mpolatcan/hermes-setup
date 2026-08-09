#!/usr/bin/env python3
"""Atomically deploy or roll back the reviewed Linear Hermes plugin.

The helper never edits Hermes config and never restarts a gateway. It exports
only a reviewed ten-file manifest from a clean, exact Git commit. Promotion and
rollback use pinned directory descriptors, a profile lock, durable coordinates,
and same-filesystem rename operations.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Callable


PLUGIN_RELATIVE = Path("integrations/linear-hermes-platform")
ALLOWLIST = (
    "__init__.py",
    "adapter.py",
    "ledger.py",
    "linear_client.py",
    "oauth_store.py",
    "mcp_client.py",
    "outbound_policy.py",
    "outbound_ledger.py",
    "linear_tools.py",
    "plugin.yaml",
)
REVIEWED_MANIFESTS: dict[str, dict[str, str]] = {
    "50be9ec8309de6cc21a894c7bae6aab31231d027": {
        "__init__.py": "0117a75173b9909b92137601e9725717c3d058c90b7551c710c94752207792a7",
        "adapter.py": "4b98f321c656c89403b35afcefa9f35b1e7226a78509df9ffbfa767137719a1f",
        "ledger.py": "2bc25766cb61152e2c302a4a87b7ece075cc495e9331d9aa0d8ae1f8859df306",
        "linear_client.py": "f6e2d0efd2ef180e030ab76c1afef35c94d349669859d370baf9e3db26f0481f",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "3a0cc6a4f492dce148f782742f6820e6cc63d07609800da2d36daf7320c1546e",
        "outbound_policy.py": "0af776d211f15ad2c1cd7a12567d7515d2bac1f26dd629e4e1f00a749c97f21d",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "17a3b1e2921280e66e7d51d2d6270633842a2f25da8c260d19ea10d6dfb26dca",
        "plugin.yaml": "80938d5975ef9e08d1a271a223db657c5037adbc7dd7f3c1a932bb2c0fe2f5f6",
    },
    "9015b1982cb28d9e76d12486996b8dcaa27a9388": {
        "__init__.py": "0117a75173b9909b92137601e9725717c3d058c90b7551c710c94752207792a7",
        "adapter.py": "7d2e06dd4b21546b4094f70a1b13914ecb6ef8378892ae543cfdbc6d069a9bc5",
        "ledger.py": "2bc25766cb61152e2c302a4a87b7ece075cc495e9331d9aa0d8ae1f8859df306",
        "linear_client.py": "f6e2d0efd2ef180e030ab76c1afef35c94d349669859d370baf9e3db26f0481f",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "3a0cc6a4f492dce148f782742f6820e6cc63d07609800da2d36daf7320c1546e",
        "outbound_policy.py": "0af776d211f15ad2c1cd7a12567d7515d2bac1f26dd629e4e1f00a749c97f21d",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "17a3b1e2921280e66e7d51d2d6270633842a2f25da8c260d19ea10d6dfb26dca",
        "plugin.yaml": "80938d5975ef9e08d1a271a223db657c5037adbc7dd7f3c1a932bb2c0fe2f5f6",
    },
    "9978787af92f441bac09d603faa06a5181bb997e": {
        "__init__.py": "0117a75173b9909b92137601e9725717c3d058c90b7551c710c94752207792a7",
        "adapter.py": "7d2e06dd4b21546b4094f70a1b13914ecb6ef8378892ae543cfdbc6d069a9bc5",
        "ledger.py": "2bc25766cb61152e2c302a4a87b7ece075cc495e9331d9aa0d8ae1f8859df306",
        "linear_client.py": "f6e2d0efd2ef180e030ab76c1afef35c94d349669859d370baf9e3db26f0481f",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "e212bed43f86846bc9e55f70425622dda4f903421e5ea529e75845f8be23ac25",
        "outbound_policy.py": "0af776d211f15ad2c1cd7a12567d7515d2bac1f26dd629e4e1f00a749c97f21d",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "17a3b1e2921280e66e7d51d2d6270633842a2f25da8c260d19ea10d6dfb26dca",
        "plugin.yaml": "80938d5975ef9e08d1a271a223db657c5037adbc7dd7f3c1a932bb2c0fe2f5f6",
    },
    "9d96f4295496982967143fc063b78146fc73348b": {
        "__init__.py": "0117a75173b9909b92137601e9725717c3d058c90b7551c710c94752207792a7",
        "adapter.py": "7d2e06dd4b21546b4094f70a1b13914ecb6ef8378892ae543cfdbc6d069a9bc5",
        "ledger.py": "2bc25766cb61152e2c302a4a87b7ece075cc495e9331d9aa0d8ae1f8859df306",
        "linear_client.py": "24a48bc832abfbf14221fe9caaa00edb242dc7b8dae6b39cb2443beff3e8fbd8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "e212bed43f86846bc9e55f70425622dda4f903421e5ea529e75845f8be23ac25",
        "outbound_policy.py": "565fde4e6e0c2dc1ccb215641d1a821b60c6be7faa779232bdc3c8fe2d6d4a50",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "22ad70c1eef8c6bd5b062daf9ba7bf9fda29ded75280ae9ce1a549278ad7f3af",
        "plugin.yaml": "80938d5975ef9e08d1a271a223db657c5037adbc7dd7f3c1a932bb2c0fe2f5f6",
    },
    "48f9c3a553d7a933b967632ca6d54a2d224b6495": {
        "__init__.py": "0117a75173b9909b92137601e9725717c3d058c90b7551c710c94752207792a7",
        "adapter.py": "764f372830cd3a6865675cda4b77da897ac58fd31274a67145ad98f2171496c1",
        "ledger.py": "2bc25766cb61152e2c302a4a87b7ece075cc495e9331d9aa0d8ae1f8859df306",
        "linear_client.py": "24a48bc832abfbf14221fe9caaa00edb242dc7b8dae6b39cb2443beff3e8fbd8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "e212bed43f86846bc9e55f70425622dda4f903421e5ea529e75845f8be23ac25",
        "outbound_policy.py": "565fde4e6e0c2dc1ccb215641d1a821b60c6be7faa779232bdc3c8fe2d6d4a50",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "22ad70c1eef8c6bd5b062daf9ba7bf9fda29ded75280ae9ce1a549278ad7f3af",
        "plugin.yaml": "8d02987e077f24ffafad08c531ac5c48826620f29a40898f3f62403d5dcdcd57",
    },
    "92bc6b1d538008b1884758ff2a91ebb1d8ba5907": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "679723f859f0a8baeb74dcba11961e5d57a9e892597da54d3b9a810dffedb3ad",
        "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
        "linear_client.py": "70bff1072ff39c28917ccd0f015985495565db3b0ddce5c9311cf84212469e99",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "3debd6bbc7ba7b6084d8bfb39045a0ed97f7a514266896f3e59bf1c0f6f0a2e7",
        "outbound_policy.py": "963e81aa311766744a005c60aa96a59bb317e3a8f674168429feb3bedb04327d",
        "outbound_ledger.py": "aa61090da20e580d12e0bd321b152dfc00123f478bde9c4954497f10c2d62b06",
        "linear_tools.py": "4d350b871eca082105979d4a4078a865493fa3f1601f25dd40e4f796919e3041",
        "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
    },
    "2fc28f4cf80b55c7a6a5f8e03ffbbb9153dfc47c": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "679723f859f0a8baeb74dcba11961e5d57a9e892597da54d3b9a810dffedb3ad",
        "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
        "linear_client.py": "70bff1072ff39c28917ccd0f015985495565db3b0ddce5c9311cf84212469e99",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "3debd6bbc7ba7b6084d8bfb39045a0ed97f7a514266896f3e59bf1c0f6f0a2e7",
        "outbound_policy.py": "963e81aa311766744a005c60aa96a59bb317e3a8f674168429feb3bedb04327d",
        "outbound_ledger.py": "aa61090da20e580d12e0bd321b152dfc00123f478bde9c4954497f10c2d62b06",
        "linear_tools.py": "eca26788b4d62866e06482dceca21dc30675cc54bba919b1495ac3a92e62abfb",
        "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
    },
    "f553c648988f870aa9de1bd8b34999c74ea05c6e": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "7d0355e3b381dcbac2e718ca0dc0f38fcfd9946a7fe0275178de587bb570be6a",
        "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
        "linear_client.py": "44f52019888b93ce0b144b09570eaf10eaba5b2593b0f11efac4fd81e6bf1189",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "3debd6bbc7ba7b6084d8bfb39045a0ed97f7a514266896f3e59bf1c0f6f0a2e7",
        "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
        "outbound_ledger.py": "aa61090da20e580d12e0bd321b152dfc00123f478bde9c4954497f10c2d62b06",
        "linear_tools.py": "b02f477d6df4cfe18e93abc80ba5851dea4fc021b733ac44a18083f836da821c",
        "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
    },
    "bf12127eb2c91c2f49a82b5f4aedde2bd17365c7": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "7d0355e3b381dcbac2e718ca0dc0f38fcfd9946a7fe0275178de587bb570be6a",
        "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
        "linear_client.py": "44f52019888b93ce0b144b09570eaf10eaba5b2593b0f11efac4fd81e6bf1189",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "3debd6bbc7ba7b6084d8bfb39045a0ed97f7a514266896f3e59bf1c0f6f0a2e7",
        "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
        "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
    },
    "ae223f9cf10c1c78fed949dcdec890582fc49610": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "7d0355e3b381dcbac2e718ca0dc0f38fcfd9946a7fe0275178de587bb570be6a",
        "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
        "linear_client.py": "44f52019888b93ce0b144b09570eaf10eaba5b2593b0f11efac4fd81e6bf1189",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
        "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
    },
    "5822ea28c36856f0ce8f244035dd489cc4a7ddda": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "1e1828faa7fdebc632d49fb505524dee598a510d969d659f5b53d62342845c61",
        "ledger.py": "910c1314a9c3489e370759f270487227af8f949bcd0d889959f9e6df8a2d0e88",
        "linear_client.py": "b1a7b1ab431af6c26d22337caa4cb70b5feef6fe886fa4fb7e0e67b1ad351158",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
        "plugin.yaml": "819912eec91576605f1fe401ce69811b335586184b4a27d5a36aa01a5ab208fb",
    },
    "c12d73119e230437faf01f0cddc294bc5f364185": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "ff9aa401976a0e41e22cdbf4604dcf7e7a292a9d0628bb30fabcb02ed9083c1c",
        "ledger.py": "910c1314a9c3489e370759f270487227af8f949bcd0d889959f9e6df8a2d0e88",
        "linear_client.py": "b1a7b1ab431af6c26d22337caa4cb70b5feef6fe886fa4fb7e0e67b1ad351158",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
        "plugin.yaml": "819912eec91576605f1fe401ce69811b335586184b4a27d5a36aa01a5ab208fb",
    },
    "d63a1e441ba3ef98c0f593116cce317a0fb566c9": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "f7695ea3d48c3f2cfe7e881467ca8211961a7f1e0b4db72e42e0956de8487a43",
        "ledger.py": "910c1314a9c3489e370759f270487227af8f949bcd0d889959f9e6df8a2d0e88",
        "linear_client.py": "7cc114f486cb99e37a1419b30abe315683931f83420e3d4d6da7e398128c5c92",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
        "plugin.yaml": "819912eec91576605f1fe401ce69811b335586184b4a27d5a36aa01a5ab208fb",
    },
    "498408a0a10082f2d1c7742f68059ffc5899b144": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "b9bce9511ecc4700165b160a6f33e98994dcefeea6d7d646045fc08503f6f41c",
        "ledger.py": "910c1314a9c3489e370759f270487227af8f949bcd0d889959f9e6df8a2d0e88",
        "linear_client.py": "7cc114f486cb99e37a1419b30abe315683931f83420e3d4d6da7e398128c5c92",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
        "plugin.yaml": "819912eec91576605f1fe401ce69811b335586184b4a27d5a36aa01a5ab208fb",
    },
    "db7fa04992a9fd3ae5c18fd1e938726f05efd4cc": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "cc89960a21e72b48e69c1b1b492e139c47d83aeaeaf53d31c2fff6b7f3dfc9fb",
        "ledger.py": "a9e1432cf2d3b3cda9f6d2d6579cfa4c2ae6c151b660803be247cbc03681d542",
        "linear_client.py": "7cc114f486cb99e37a1419b30abe315683931f83420e3d4d6da7e398128c5c92",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
        "plugin.yaml": "299390e58eb8e4a00e7350a33ecf5dc8908786c375b5c8ccbad992736f119d93",
    },
    "87868f2d3fcb27541398df1671e6b6ea8698cf59": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "cc89960a21e72b48e69c1b1b492e139c47d83aeaeaf53d31c2fff6b7f3dfc9fb",
        "ledger.py": "a9e1432cf2d3b3cda9f6d2d6579cfa4c2ae6c151b660803be247cbc03681d542",
        "linear_client.py": "bb995c1eeccf0a91cda57c48e3787dce575c26f10e3fa2c13ded80da19dab920",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "29e7f91c9ef0e7b302f369d6aea49f0d6137a281d57a6df20eec2e1594ae9e46",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "c1d5f920aff8b0df299728d2d8c621ecd517bac917372d913cee6da7b032bf08",
        "plugin.yaml": "299390e58eb8e4a00e7350a33ecf5dc8908786c375b5c8ccbad992736f119d93",
    },
    "2f9aaabcfb0a3d080a1078c1506a000a20024190": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "771c78c3e420dcc7667163794ceacd9dd026ffa74015fd2df2fe439cfcc750d5",
        "ledger.py": "ac00c13e3d62da2a81d2c6f89ea98a6405911886c3b848e8d3300735b0ee21d1",
        "linear_client.py": "91084e4ee0b83fdaa20260bc2cf0cab8b4ad944265cb3882349de733f97eee4a",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "29e7f91c9ef0e7b302f369d6aea49f0d6137a281d57a6df20eec2e1594ae9e46",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "c1d5f920aff8b0df299728d2d8c621ecd517bac917372d913cee6da7b032bf08",
        "plugin.yaml": "ad0c41f5c2e93a2a37b6ee379d48a0f7578791cf841651092caa86648be98881",
    },
    "111dad9039caf8b7b9103d67cbb74335101e7338": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "771c78c3e420dcc7667163794ceacd9dd026ffa74015fd2df2fe439cfcc750d5",
        "ledger.py": "ac00c13e3d62da2a81d2c6f89ea98a6405911886c3b848e8d3300735b0ee21d1",
        "linear_client.py": "2ce52cbf1c1e6226ecb0125d1d8c0ff7232131a8cab9415cd9851008e16bd8c8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "70327e431a2059e959e0aa8102cb24ec30c98be25006d8e8873034f66e726c81",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "8971a7d8a1e1a98d676de3085efadc4b88324a38526d2b753b2edb63096f056a",
        "plugin.yaml": "ad0c41f5c2e93a2a37b6ee379d48a0f7578791cf841651092caa86648be98881",
    },
    "05ac704b18630625fc49622b2b1df2eeb6cf7b57": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "a40a0344ae6f0d30bd7c9e0dc833b9395539538b2ae476d7849eecb0ab9afdba",
        "ledger.py": "ac00c13e3d62da2a81d2c6f89ea98a6405911886c3b848e8d3300735b0ee21d1",
        "linear_client.py": "2ce52cbf1c1e6226ecb0125d1d8c0ff7232131a8cab9415cd9851008e16bd8c8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "70327e431a2059e959e0aa8102cb24ec30c98be25006d8e8873034f66e726c81",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "8971a7d8a1e1a98d676de3085efadc4b88324a38526d2b753b2edb63096f056a",
        "plugin.yaml": "9ce3a34baaaa2997149bc882e35b083d5f600ec087cbe48103d5270bf65225ee",
    },
    "74b03613de9a9be440239f7a25534e46f349f374": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "af60b481b964f6287d0ce792f904cdeeca14fb994f5dde569f413d6dc85df1f6",
        "ledger.py": "ac00c13e3d62da2a81d2c6f89ea98a6405911886c3b848e8d3300735b0ee21d1",
        "linear_client.py": "2ce52cbf1c1e6226ecb0125d1d8c0ff7232131a8cab9415cd9851008e16bd8c8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
        "outbound_policy.py": "70327e431a2059e959e0aa8102cb24ec30c98be25006d8e8873034f66e726c81",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "8971a7d8a1e1a98d676de3085efadc4b88324a38526d2b753b2edb63096f056a",
        "plugin.yaml": "fbcc5c7c01c393eb80be0c8f335e1ecc65b38d9309f2bfaa4f9e572dcf8afd9f",
    },
    "571013eddb8b1a81a5b52336d6938eb0a010fb9c": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "af60b481b964f6287d0ce792f904cdeeca14fb994f5dde569f413d6dc85df1f6",
        "ledger.py": "ac00c13e3d62da2a81d2c6f89ea98a6405911886c3b848e8d3300735b0ee21d1",
        "linear_client.py": "2ce52cbf1c1e6226ecb0125d1d8c0ff7232131a8cab9415cd9851008e16bd8c8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "0367e60ae11cf93a564db84b4b2845428b730f8d012d176ba09639f019765ec8",
        "outbound_policy.py": "565fde4e6e0c2dc1ccb215641d1a821b60c6be7faa779232bdc3c8fe2d6d4a50",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "22ad70c1eef8c6bd5b062daf9ba7bf9fda29ded75280ae9ce1a549278ad7f3af",
        "plugin.yaml": "fbcc5c7c01c393eb80be0c8f335e1ecc65b38d9309f2bfaa4f9e572dcf8afd9f",
    },
    "aeeb3243ff2d13938bc25f993965b7916b0bc257": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "af60b481b964f6287d0ce792f904cdeeca14fb994f5dde569f413d6dc85df1f6",
        "ledger.py": "ac00c13e3d62da2a81d2c6f89ea98a6405911886c3b848e8d3300735b0ee21d1",
        "linear_client.py": "2ce52cbf1c1e6226ecb0125d1d8c0ff7232131a8cab9415cd9851008e16bd8c8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "75933ce39f7aa3d1aa852d9c4d705ebc88e28a809d09e29efca42ca87cd583b8",
        "outbound_policy.py": "565fde4e6e0c2dc1ccb215641d1a821b60c6be7faa779232bdc3c8fe2d6d4a50",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "22ad70c1eef8c6bd5b062daf9ba7bf9fda29ded75280ae9ce1a549278ad7f3af",
        "plugin.yaml": "fbcc5c7c01c393eb80be0c8f335e1ecc65b38d9309f2bfaa4f9e572dcf8afd9f",
    },
    "fa70d9fd43a1fcb07db095fae53c593db742af1b": {
        "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
        "adapter.py": "af60b481b964f6287d0ce792f904cdeeca14fb994f5dde569f413d6dc85df1f6",
        "ledger.py": "ac00c13e3d62da2a81d2c6f89ea98a6405911886c3b848e8d3300735b0ee21d1",
        "linear_client.py": "2ce52cbf1c1e6226ecb0125d1d8c0ff7232131a8cab9415cd9851008e16bd8c8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "e212bed43f86846bc9e55f70425622dda4f903421e5ea529e75845f8be23ac25",
        "outbound_policy.py": "565fde4e6e0c2dc1ccb215641d1a821b60c6be7faa779232bdc3c8fe2d6d4a50",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "22ad70c1eef8c6bd5b062daf9ba7bf9fda29ded75280ae9ce1a549278ad7f3af",
        "plugin.yaml": "fbcc5c7c01c393eb80be0c8f335e1ecc65b38d9309f2bfaa4f9e572dcf8afd9f",
    },
    "a0f815d0ad378ee421d025bc67753c705d8db48c": {
        "__init__.py": "90565b9f72024822d3a0aa595d7f739c1bd2622730a68078de18b99c74d0a888",
        "adapter.py": "4e058c8f7a12989baca2c7ade16db9f7bc466a55c0351f4c46dd82e39d12ed25",
        "ledger.py": "c039c8b321c0a2b487a897a226375c3eda99075a3ed2291b28b9630bbefb85cf",
        "linear_client.py": "2ce52cbf1c1e6226ecb0125d1d8c0ff7232131a8cab9415cd9851008e16bd8c8",
        "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
        "mcp_client.py": "e212bed43f86846bc9e55f70425622dda4f903421e5ea529e75845f8be23ac25",
        "outbound_policy.py": "565fde4e6e0c2dc1ccb215641d1a821b60c6be7faa779232bdc3c8fe2d6d4a50",
        "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
        "linear_tools.py": "22ad70c1eef8c6bd5b062daf9ba7bf9fda29ded75280ae9ce1a549278ad7f3af",
        "plugin.yaml": "e77a1592959cd7de3157894f79a4c2126ae29d44d839a9c20a1e84f39153aeaf",
    },
}
_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_HANDLED_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)


class DeploymentError(RuntimeError):
    """Fail-closed deployment error safe for operator output."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_git(repo_root: Path, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=not binary,
        timeout=30,
    )
    if result.returncode != 0:
        raise DeploymentError(f"Git command failed: {' '.join(args[:2])}")
    return result.stdout


def _validate_dir_info(info: os.stat_result, label: str, *, exact_mode: int | None = None) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise DeploymentError(f"Directory type or owner is invalid: {label}")
    mode = info.st_mode & 0o777
    if exact_mode is not None:
        if mode != exact_mode:
            raise DeploymentError(f"Directory mode is invalid: {label}")
    elif mode & 0o022:
        raise DeploymentError(f"Directory permissions are unsafe: {label}")


def _open_absolute_dir(path: Path, label: str) -> tuple[Path, int]:
    try:
        canonical = path.resolve(strict=True)
        fd = os.open(canonical, _DIR_FLAGS)
    except OSError as exc:
        raise DeploymentError(f"Required directory is unavailable: {label}") from exc
    try:
        _validate_dir_info(os.fstat(fd), label)
        return canonical, fd
    except Exception:
        os.close(fd)
        raise


def _open_child_dir(parent_fd: int, name: str, label: str, *, exact_mode: int | None = None) -> int:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise DeploymentError(f"Directory must be real and non-symlink: {label}") from exc
    try:
        _validate_dir_info(os.fstat(fd), label, exact_mode=exact_mode)
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_profile_roots(profiles_root: Path, profile: str) -> tuple[Path, int, int, int, int]:
    if not _PROFILE_RE.fullmatch(profile):
        raise DeploymentError("Profile name is invalid")
    canonical, profiles_fd = _open_absolute_dir(profiles_root, "profiles root")
    try:
        profile_fd = _open_child_dir(profiles_fd, profile, "profile root")
        try:
            plugins_fd = _open_child_dir(profile_fd, "plugins", "plugins root")
            try:
                state_fd = _open_child_dir(profile_fd, "state", "state root")
            except Exception:
                os.close(plugins_fd)
                raise
        except Exception:
            os.close(profile_fd)
            raise
    except Exception:
        os.close(profiles_fd)
        raise
    return canonical, profiles_fd, profile_fd, plugins_fd, state_fd


def _close_many(*fds: int | None) -> None:
    for fd in fds:
        if fd is not None and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise DeploymentError("Short write while staging plugin")
        view = view[written:]


def _tree_records_fd(directory_fd: int, prefix: str = "") -> list[str]:
    records: list[str] = []
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise DeploymentError("Plugin tree could not be enumerated") from exc
    for name in names:
        if name in {".", ".."} or "/" in name or "\0" in name:
            raise DeploymentError("Plugin tree contains an invalid entry name")
        relative = f"{prefix}/{name}" if prefix else name
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise DeploymentError("Plugin tree entry is unavailable") from exc
        mode = info.st_mode & 0o777
        if stat.S_ISLNK(info.st_mode):
            raise DeploymentError("Plugin tree contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_child_dir(directory_fd, name, relative)
            try:
                records.append(f"d\0{relative}\0{mode:o}")
                records.extend(_tree_records_fd(child_fd, relative))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(info.st_mode):
            try:
                file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
            except OSError as exc:
                raise DeploymentError("Plugin tree file is unavailable") from exc
            try:
                pinned = os.fstat(file_fd)
                if pinned.st_dev != info.st_dev or pinned.st_ino != info.st_ino:
                    raise DeploymentError("Plugin tree entry changed during verification")
                records.append(f"f\0{relative}\0{mode:o}\0{_sha256(_read_all(file_fd))}")
            finally:
                os.close(file_fd)
        else:
            raise DeploymentError("Plugin tree contains an unsupported entry")
    return records


def _tree_digest_fd(directory_fd: int) -> str:
    return _sha256("\n".join(_tree_records_fd(directory_fd)).encode())


def _verify_candidate_fd(directory_fd: int, manifest: dict[str, str]) -> None:
    _validate_dir_info(os.fstat(directory_fd), "candidate", exact_mode=0o700)
    if set(manifest) != set(ALLOWLIST):
        raise DeploymentError("Reviewed manifest does not match the deployment allowlist")
    try:
        entries = set(os.listdir(directory_fd))
    except OSError as exc:
        raise DeploymentError("Candidate entry set is unavailable") from exc
    if entries != set(ALLOWLIST):
        raise DeploymentError("Candidate entry set does not match the deployment allowlist")
    for name in ALLOWLIST:
        try:
            file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
        except OSError as exc:
            raise DeploymentError(f"Candidate entry is unavailable: {name}") from exc
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
                raise DeploymentError(f"Candidate entry mode, type or owner is invalid: {name}")
            if _sha256(_read_all(file_fd)) != manifest[name]:
                raise DeploymentError(f"Candidate hash mismatch: {name}")
        finally:
            os.close(file_fd)


def _acquire_lock(state_fd: int, timeout: float) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open("linear-plugin-deploy.lock", flags, 0o600, dir_fd=state_fd)
    except OSError as exc:
        raise DeploymentError("Deployment lock is unavailable") from exc
    try:
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise DeploymentError("Deployment lock owner or type is invalid")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeploymentError("Deployment lock timed out")
                time.sleep(0.05)
    except Exception:
        os.close(fd)
        raise


def _unique_name(prefix: str) -> str:
    return f"{prefix}{int(time.time())}-{secrets.token_hex(6)}"


def _mkdir_private(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        fd = _open_child_dir(parent_fd, name, name, exact_mode=0o700)
        os.fsync(parent_fd)
        return fd
    except OSError as exc:
        raise DeploymentError("Private deployment directory could not be created") from exc


def _remove_tree_fd(parent_fd: int, name: str) -> None:
    try:
        child_fd = _open_child_dir(parent_fd, name, name)
    except DeploymentError:
        return
    try:
        for entry in os.listdir(child_fd):
            info = os.stat(entry, dir_fd=child_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                _remove_tree_fd(child_fd, entry)
            else:
                os.unlink(entry, dir_fd=child_fd)
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(child_fd)


def _write_coordinate_record(state_fd: int, name: str, payload: dict[str, str]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=state_fd)
    except OSError as exc:
        raise DeploymentError("Rollback coordinate record could not be created") from exc
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, (json.dumps(payload, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(state_fd)


def _install_signal_guards(callback: Callable[[int], bool]) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        if callback(signum):
            raise DeploymentError(f"Deployment interrupted by signal {signum}")

    for signum in _HANDLED_SIGNALS:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)
    return previous


def _restore_signal_guards(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def deploy_reviewed(
    *,
    repo_root: Path,
    profiles_root: Path,
    profile: str,
    commit: str,
    lock_timeout: float = 8.0,
    announce: Callable[[dict[str, str]], None] | None = None,
    _post_promote_hook: Callable[[Path], None] | None = None,
    _after_backup_hook: Callable[[], None] | None = None,
    _after_verified_hook: Callable[[], None] | None = None,
) -> dict[str, str]:
    repo_root = repo_root.resolve(strict=True)
    _, profiles_fd, profile_fd, plugins_fd, state_fd = _open_profile_roots(profiles_root, profile)
    lock_fd: int | None = None
    stage_fd: int | None = None
    target_fd: int | None = None
    stage_name: str | None = None
    rollback_name: str | None = None
    failed_name: str | None = None
    previous_handlers: dict[int, Any] = {}
    state = "preparing"
    recovering = False

    try:
        manifest = REVIEWED_MANIFESTS.get(commit)
        if manifest is None:
            raise DeploymentError("Commit has no reviewed deployment manifest")
        if set(manifest) != set(ALLOWLIST):
            raise DeploymentError("Reviewed manifest is incomplete")
        resolved = str(_run_git(repo_root, "rev-parse", f"{commit}^{{commit}}")).strip()
        if resolved != commit:
            raise DeploymentError("Commit must be an exact full reviewed SHA")
        if str(_run_git(repo_root, "status", "--porcelain", "--untracked-files=all")).strip():
            raise DeploymentError("Repository worktree is not clean")

        lock_fd = _acquire_lock(state_fd, lock_timeout)
        target_fd = _open_child_dir(plugins_fd, "linear", "linear target")
        pinned_target = os.fstat(target_fd)

        def recover(_signum: int = 0) -> bool:
            nonlocal recovering, failed_name, state
            if recovering:
                return True
            if state in {"verified", "recovered"}:
                return False
            recovering = True
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _HANDLED_SIGNALS)
            try:
                names = set(os.listdir(plugins_fd))
                if rollback_name and rollback_name in names:
                    if "linear" in names:
                        failed_name = _unique_name(".linear-failed-")
                        os.rename("linear", failed_name, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
                        os.fsync(plugins_fd)
                    os.rename(rollback_name, "linear", src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
                    os.fsync(plugins_fd)
                    restored_fd = _open_child_dir(plugins_fd, "linear", "restored target")
                    try:
                        restored = os.fstat(restored_fd)
                        if restored.st_dev != pinned_target.st_dev or restored.st_ino != pinned_target.st_ino:
                            raise DeploymentError("Recovered target inode does not match pinned rollback")
                    finally:
                        os.close(restored_fd)
                    state = "recovered"
                return True
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                recovering = False

        previous_handlers = _install_signal_guards(recover)
        stage_name = _unique_name(".linear-stage-")
        stage_fd = _mkdir_private(plugins_fd, stage_name)
        for name in ALLOWLIST:
            data = _run_git(repo_root, "show", f"{commit}:{(PLUGIN_RELATIVE / name).as_posix()}", binary=True)
            assert isinstance(data, bytes)
            if _sha256(data) != manifest[name]:
                raise DeploymentError(f"Reviewed source hash mismatch: {name}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(name, flags, 0o600, dir_fd=stage_fd)
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        os.fsync(stage_fd)
        _verify_candidate_fd(stage_fd, manifest)
        state = "staged"

        rollback_name = _unique_name(".linear-rollback-")
        if rollback_name in set(os.listdir(plugins_fd)):
            raise DeploymentError("Rollback slot already exists")
        rollback_digest = _tree_digest_fd(target_fd)
        rollback_path = str(profiles_root.resolve(strict=True) / profile / "plugins" / rollback_name)
        record_name = f"linear-plugin-deploy-{rollback_name.removeprefix('.linear-rollback-')}.json"
        coordinates = {
            "status": "prepared",
            "profile": profile,
            "commit": commit,
            "rollback_path": rollback_path,
            "rollback_digest": rollback_digest,
        }
        _write_coordinate_record(state_fd, record_name, coordinates)
        if announce is not None:
            announce(dict(coordinates))

        os.rename("linear", rollback_name, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
        os.fsync(plugins_fd)
        rollback_fd = _open_child_dir(plugins_fd, rollback_name, "rollback slot")
        try:
            moved = os.fstat(rollback_fd)
            if moved.st_dev != pinned_target.st_dev or moved.st_ino != pinned_target.st_ino:
                raise DeploymentError("Rollback inode changed during promotion")
            os.fchmod(rollback_fd, 0o700)
            os.fsync(rollback_fd)
        finally:
            os.close(rollback_fd)
        os.fsync(plugins_fd)
        state = "backed_up"
        if _after_backup_hook is not None:
            _after_backup_hook()

        os.rename(stage_name, "linear", src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
        os.fsync(plugins_fd)
        state = "promoted"
        os.close(stage_fd)
        stage_fd = None
        promoted_fd = _open_child_dir(plugins_fd, "linear", "promoted target", exact_mode=0o700)
        try:
            if _post_promote_hook is not None:
                _post_promote_hook(profiles_root.resolve(strict=True) / profile / "plugins" / "linear")
            _verify_candidate_fd(promoted_fd, manifest)
            target_digest = _tree_digest_fd(promoted_fd)
        finally:
            os.close(promoted_fd)
        state = "verified"
        if _after_verified_hook is not None:
            _after_verified_hook()
        return {
            **coordinates,
            "status": "verified",
            "target_path": str(profiles_root.resolve(strict=True) / profile / "plugins" / "linear"),
            "target_digest": target_digest,
            "record_path": str(profiles_root.resolve(strict=True) / profile / "state" / record_name),
        }
    except BaseException:
        if "recover" in locals():
            recover()
        raise
    finally:
        if previous_handlers:
            _restore_signal_guards(previous_handlers)
        if stage_fd is not None:
            os.close(stage_fd)
        if target_fd is not None:
            os.close(target_fd)
        if stage_name is not None and stage_name in set(os.listdir(plugins_fd)):
            _remove_tree_fd(plugins_fd, stage_name)
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        _close_many(state_fd, plugins_fd, profile_fd, profiles_fd)


def rollback_exact(
    *,
    profiles_root: Path,
    profile: str,
    rollback_path: Path,
    rollback_digest: str,
    lock_timeout: float = 8.0,
    _post_restore_hook: Callable[[Path], None] | None = None,
    _after_current_backup_hook: Callable[[], None] | None = None,
    _during_recovery_hook: Callable[[], None] | None = None,
) -> dict[str, str]:
    canonical, profiles_fd, profile_fd, plugins_fd, state_fd = _open_profile_roots(profiles_root, profile)
    lock_fd: int | None = None
    rollback_fd: int | None = None
    target_fd: int | None = None
    failed_name: str | None = None
    verified = False
    recovering = False
    previous_handlers: dict[int, Any] = {}
    try:
        lock_fd = _acquire_lock(state_fd, lock_timeout)
        expected_parent = canonical / profile / "plugins"
        supplied = rollback_path.absolute()
        if supplied.parent != expected_parent or not supplied.name.startswith(".linear-rollback-"):
            raise DeploymentError("Rollback coordinates are outside the named profile")
        if not re.fullmatch(r"[0-9a-f]{64}", rollback_digest):
            raise DeploymentError("Rollback digest is invalid")
        rollback_fd = _open_child_dir(plugins_fd, supplied.name, "rollback slot", exact_mode=0o700)
        if _tree_digest_fd(rollback_fd) != rollback_digest:
            raise DeploymentError("Rollback tree digest does not match")
        pinned_rollback = os.fstat(rollback_fd)
        target_fd = _open_child_dir(plugins_fd, "linear", "current target")
        pinned_target = os.fstat(target_fd)

        def interrupt_rollback(_signum: int) -> bool:
            return not (verified or recovering)

        previous_handlers = _install_signal_guards(interrupt_rollback)
        failed_name = _unique_name(".linear-failed-")
        os.rename("linear", failed_name, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
        os.fsync(plugins_fd)
        moved_current_fd = _open_child_dir(plugins_fd, failed_name, "preserved current")
        try:
            moved = os.fstat(moved_current_fd)
            if moved.st_dev != pinned_target.st_dev or moved.st_ino != pinned_target.st_ino:
                raise DeploymentError("Current target inode changed during rollback")
        finally:
            os.close(moved_current_fd)
        if _after_current_backup_hook is not None:
            _after_current_backup_hook()

        os.rename(supplied.name, "linear", src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
        os.fsync(plugins_fd)
        restored_fd = _open_child_dir(plugins_fd, "linear", "restored rollback")
        try:
            moved = os.fstat(restored_fd)
            if moved.st_dev != pinned_rollback.st_dev or moved.st_ino != pinned_rollback.st_ino:
                raise DeploymentError("Rollback inode changed during restore")
            if _post_restore_hook is not None:
                _post_restore_hook(expected_parent / "linear")
            if _tree_digest_fd(restored_fd) != rollback_digest:
                raise DeploymentError("Restored rollback digest does not match")
        finally:
            os.close(restored_fd)
        verified = True
        return {
            "status": "rolled_back",
            "profile": profile,
            "target_path": str(expected_parent / "linear"),
            "failed_path": str(expected_parent / failed_name),
            "rollback_digest": rollback_digest,
        }
    except BaseException:
        recovering = True
        try:
            if failed_name is not None:
                names = set(os.listdir(plugins_fd))
                if failed_name in names:
                    if "linear" in names:
                        rejected = _unique_name(".linear-rollback-failed-")
                        os.rename("linear", rejected, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
                        os.fsync(plugins_fd)
                    if _during_recovery_hook is not None:
                        _during_recovery_hook()
                    os.rename(failed_name, "linear", src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
                    os.fsync(plugins_fd)
        finally:
            recovering = False
        raise
    finally:
        if previous_handlers:
            _restore_signal_guards(previous_handlers)
        _close_many(target_fd, rollback_fd)
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        _close_many(state_fd, plugins_fd, profile_fd, profiles_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    deploy = sub.add_parser("deploy")
    deploy.add_argument("--repo-root", type=Path, required=True)
    deploy.add_argument("--profiles-root", type=Path, required=True)
    deploy.add_argument("--profile", required=True)
    deploy.add_argument("--commit", required=True)
    deploy.add_argument("--lock-timeout", type=float, default=8.0)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--profiles-root", type=Path, required=True)
    rollback.add_argument("--profile", required=True)
    rollback.add_argument("--rollback-path", type=Path, required=True)
    rollback.add_argument("--rollback-digest", required=True)
    rollback.add_argument("--lock-timeout", type=float, default=8.0)
    return parser


def _announce(payload: dict[str, str]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)
    try:
        os.fsync(sys.stdout.fileno())
    except OSError:
        pass


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.action == "deploy":
            result = deploy_reviewed(
                repo_root=args.repo_root,
                profiles_root=args.profiles_root,
                profile=args.profile,
                commit=args.commit,
                lock_timeout=args.lock_timeout,
                announce=_announce,
            )
        else:
            result = rollback_exact(
                profiles_root=args.profiles_root,
                profile=args.profile,
                rollback_path=args.rollback_path,
                rollback_digest=args.rollback_digest,
                lock_timeout=args.lock_timeout,
            )
    except DeploymentError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
