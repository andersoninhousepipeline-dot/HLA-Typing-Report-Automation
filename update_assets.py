import re

with open('hla_assets.py', 'r') as f:
    content = f.read()

with open('new_nabl_b64_formatted.txt', 'r') as f:
    new_b64_lines = f.read()

# Pattern to find NABL_SEAL_DEMOG_B64 block
# It starts with NABL_SEAL_DEMOG_B64 = ( and ends with )
pattern = r'NABL_SEAL_DEMOG_B64 = \(\n.*?\n\)'
replacement = f'NABL_SEAL_DEMOG_B64 = (\n{new_b64_lines}\n)'

# Check if pattern matches
if re.search(pattern, content, re.DOTALL):
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    with open('hla_assets.py', 'w') as f:
        f.write(new_content)
    print("Successfully updated hla_assets.py")
else:
    print("Could not find NABL_SEAL_DEMOG_B64 block in hla_assets.py")
