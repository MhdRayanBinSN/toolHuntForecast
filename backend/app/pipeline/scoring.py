import math
import re

def candidate_score(candidate: dict, category: str, index: int=0, liveness: float=0.5, keywords=None) -> dict:
    text=(candidate.get("name","")+" "+candidate.get("tagline","")+" "+candidate.get("description","")).lower()
    phrases=[category,*(keywords or [])]
    text_words=set(re.findall(r"[a-z0-9]+",text))
    fits=[]
    for phrase in phrases:
        words=[w for w in re.findall(r"[a-z0-9]+",str(phrase).lower()) if len(w)>2]
        if words: fits.append(sum(w in text_words for w in words)/len(words))
    fit=max(fits,default=.3)
    novelty=.5; velocity=min(1,float(candidate.get("votes",0))/100)
    source_count=1/3 if candidate.get("source") else 0
    live=1.0 if liveness else 0.0
    total=.30*fit+.25*novelty+.15*velocity+.15*source_count+.15*live
    return {"category_fit":fit,"novelty":novelty,"velocity":velocity,"source_count":source_count,"liveness":live,"total":total}
