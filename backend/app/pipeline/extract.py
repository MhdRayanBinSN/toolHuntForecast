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
    for field, pattern in [("pricing",r"(?:\$|€|£|₹)\s?\d[\d,.]*(?:\s*/\s*(?:mo|month|year))?|free plan|contact sales"),("features",r"[^　.!?]{20,120}(?:automate|integrat|workflow|analytics|campaign|feature)[^　.!?]{0,100}")]:
        m=re.search(pattern,text,re.I)
        if m:
            quote=m.group(0).strip(); facts.append({"field":field,"value":quote,"quote":quote,"source_url":url,"kind":"claim"})
    headings=[n.text(strip=True) for n in tree.css("h1,h2,h3") if n.text(strip=True)]
    return {"title":title,"description":meta.get("description",meta.get("og:description","")),"text":text[:25000],"headings":headings,"facts":facts,"hash":hashlib.sha256(text.encode()).hexdigest()}
