import math

def candidate_score(candidate: dict, category: str, index: int=0, liveness: float=0.5) -> dict:
    text=(candidate.get("name","")+" "+candidate.get("tagline","")+" "+candidate.get("description","")).lower()
    words=[w for w in category.lower().split() if len(w)>2]
    fit=sum(w in text for w in words)/max(1,len(words))
    fit=max(.3,fit) if not words else fit
    novelty=.5; velocity=min(1,float(candidate.get("votes",0))/100)
    source_count=1/3 if candidate.get("source") else 0
    live=1.0 if liveness else 0.0
    total=.30*fit+.25*novelty+.15*velocity+.15*source_count+.15*live
    return {"category_fit":fit,"novelty":novelty,"velocity":velocity,"source_count":source_count,"liveness":live,"total":total}
