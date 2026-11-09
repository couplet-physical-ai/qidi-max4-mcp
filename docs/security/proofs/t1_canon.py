from _h import *
p = mk()
fp = ConfirmationBroker.fingerprint
cases = [
 ("int vs float", {"a":1}, {"a":1.0}),
 ("int vs str",   {"a":1}, {"a":"1"}),
 ("bool vs int",  {"force":True}, {"force":1}),
 ("dict order",   {"a":1,"b":2}, {"b":2,"a":1}),
 ("nested order", {"p":{"x":1,"y":2}}, {"p":{"y":2,"x":1}}),
 ("None vs 'None' (default=str)", {"a":None}, {"a":"None"}),
 ("tuple vs list", {"a":(1,2)}, {"a":[1,2]}),
 ("set vs str",   {"a":{1}}, {"a":"{1}"}),
 ("obj vs str(obj)", {"a":Risk.DESTRUCTIVE}, {"a":"destructive"}),
 ("bytes vs str", {"a":b"x"}, {"a":"b'x'"}),
 ("float 1e2 vs 100.0", {"a":1e2}, {"a":100.0}),
 ("str '100' vs int 100", {"target":"100"}, {"target":100}),
 ("nan vs nan", {"a":float('nan')}, {"a":float('nan')}),
 ("confirm stripped", {"a":1,"confirm":True}, {"a":1,"confirm":False}),
 ("extra key", {"path":"a.gcode"}, {"path":"a.gcode","root":"config"}),
 ("case", {"path":"A.gcode"}, {"path":"a.gcode"}),
 ("path traversal", {"path":"x/../y.gcode"}, {"path":"y.gcode"}),
]
for name, a, b in cases:
    fa, fb = fp("t", a), fp("t", b)
    print(f"{'COLLIDE ' if fa==fb else 'distinct'} | {name}: {fa} vs {fb}")
# tool name in fingerprint
print("tool distinct:", fp("delete_file", {"path":"x"}) != fp("delete_directory", {"path":"x"}))
