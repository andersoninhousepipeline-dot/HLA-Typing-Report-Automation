import os
with open('new_nabl_b64.txt', 'r') as f:
    b64 = f.read().strip()
lines = [b64[i:i+80] for i in range(0, len(b64), 80)]
formatted = '    "' + '"\n    "'.join(lines) + '"'
with open('new_nabl_b64_formatted.txt', 'w') as f:
    f.write(formatted)
