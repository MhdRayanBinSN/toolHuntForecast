import re, json, hashlib
from selectolax.lexbor import LexborHTMLParser as HTMLParser

def extract(html: str, url: str):
    tree=HTMLParser(html); title=tree.css_first("title"); title=title.text(strip=True) if title else ""
    for node in tree.css("script,style,noscript,svg"): node.decompose()
    text=" ".join(tree.body.text(separator=" ",strip=True).split()) if tree.body else " ".join(tree.text().split())
    meta={}
    for n in tree.css("meta"):
        key=n.attributes.get("property") or n.attributes.get("name")
        if key and n.attributes.get("content"): meta[key.lower()]=n.attributes["content"].strip()
    facts=[]
    sentences=[s.strip() for s in re.split(r"(?<=[.!?])\s+|\s*[•\n]+",text) if 20<=len(s.strip())<=400]
    patterns=[
        ("pricing",r"(?:\$|€|£|₹)\s?\d[\d,.]*(?:\s*/\s*(?:mo|month|year))?|free plan|contact sales|free trial"),
        ("features",r"automate|workflow|analytics|campaign|feature|sequenc|personaliz|warmup|mailbox"),
        ("integrations",r"integrat|\bAPI\b|webhook|Zapier|Salesforce|HubSpot|Slack|CRM"),
        ("security",r"security|SOC\s?2|GDPR|encryption|compliance|privacy|SSO|SAML"),
        ("limits",r"limit|up to|maximum|max\.?\s+\d|per day|per month|seats|users|credits|emails? per"),
        ("limitations",r"not supported|not available|does not|doesn't|cannot|can't|limited|limitation|requires (?:a|an|the)|not include")
    ]
    used_sentences=set()
    for field,pattern in patterns:
        quote=next((sentence for sentence in sentences if sentence not in used_sentences and re.search(pattern,sentence,re.I)),"")
        if quote:
            used_sentences.add(quote)
            facts.append({"field":field,"value":quote,"quote":quote,"source_url":url,"kind":"claim"})
    headings=[n.text(strip=True) for n in tree.css("h1,h2,h3") if n.text(strip=True)]
    return {"title":title,"description":meta.get("description",meta.get("og:description","")),"text":text[:25000],"headings":headings,"facts":facts,"hash":hashlib.sha256(text.encode()).hexdigest()}
