import json
rt = json.load(open("output/rating_transitions.json"))
block = rt["10K_XC"]["M"]["FR_to_SO"]
print(list(block.keys()))
print("improvements[:5]          ", block.get("improvements", [])[:5])
print("improvements_discounted[:5]", block.get("improvements_discounted", [])[:5])

yt = json.load(open("output/yearly_trends.json"))
print("yearly_trends keys:", list(yt.keys()))
print("10K_XC M yearly:", yt.get("10K_XC", {}).get("M"))
