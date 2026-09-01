"""One-off probe: does the SDK's account.getQuota RPC expose the same
'Plan: N/M (X% used)' figure copilot-cli's statusline shows?

Usage: python _bench/quota_probe.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copilot import CopilotClient
from copilot.rpc import AccountGetQuotaRequest


async def main():
    client = CopilotClient(working_directory=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    await client.start()
    st = await client.get_auth_status()
    print(f"auth: login={st.login} host={st.host}")
    try:
        res = await client.rpc.account.get_quota(AccountGetQuotaRequest())
        print("quota_snapshots:")
        for k, v in res.quota_snapshots.items():
            print(f"  {k}: {v.to_dict()}")
    except Exception as e:
        print(f"get_quota FAILED: {type(e).__name__}: {e}")
    await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
