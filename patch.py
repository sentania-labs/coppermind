import re
with open("coppermind/schema.py", "r") as f:
    content = f.read()
print("original length:", len(content))
