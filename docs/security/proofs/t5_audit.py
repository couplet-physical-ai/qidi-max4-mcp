from _h import *
import json, tempfile, os
D = Risk.DESTRUCTIVE
# mkdtemp, not mktemp: the audit logger creates the file itself, so this needs a
# path that does not exist yet -- but mktemp leaves a window where another process
# can win the race to that name. A private 0700 directory closes it. (CodeQL
# py/insecure-temporary-file, high. Flagged in a *security proof* script, which
# is its own small lesson.)
path = os.path.join(tempfile.mkdtemp(prefix="qidi-proof-"), "audit.jsonl")
p = mk(QIDI_AUDIT_LOG=path)
# 1. brute-force / wrong token attempts: are they logged?
for i in range(5):
    try: p.authorize("reboot_printer", D, {}, confirm_token=f"qidi-guess{i}")
    except QidiError: pass
print("after 5 bad-token attempts, audit tail:", [(e['outcome'], e['detail']) for e in p.audit.tail()])
# 2. mismatched-args token attempt: logged?
tok = p.authorize("delete_file", D, {"path":"a"})["confirm_token"]
try: p.authorize("delete_file", D, {"path":"printer.cfg"}, confirm_token=tok)
except QidiError: pass
print("after mismatched redeem:", [(e['outcome'], e['detail']) for e in p.audit.tail()])
# 3. successful redeem: is the redemption itself logged by authorize()?
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
n = len(p.audit.tail()); p.authorize("reboot_printer", D, {}, confirm_token=tok)
print("successful redeem added audit lines:", len(p.audit.tail()) - n)
# 4. is the token in the log redacted?
lines = open(path).read().splitlines()
issued = [json.loads(l) for l in lines if json.loads(l)["outcome"]=="ticket_issued"]
print("ticket_issued detail field on disk:", issued[-1]["detail"], "| redeemable? ->", issued[-1]["detail"] == tok)
print("args redaction check:", p.audit.record("x", D, {"confirm_token": "qidi-abc"}, "ok") or p.audit.tail(1)[0]["args"])
os.unlink(path); os.rmdir(os.path.dirname(path))
# 5. unwritable audit path: does the gate still clear actions?
p2 = mk(QIDI_AUDIT_LOG="/nonexistent_root_dir_zzz/sub/audit.jsonl")
tok = p2.authorize("reboot_printer", D, {})["confirm_token"]
print("unwritable audit path -> redeem:", p2.authorize("reboot_printer", D, {}, confirm_token=tok), "(None=CLEARED); file exists?", os.path.exists("/nonexistent_root_dir_zzz"))
