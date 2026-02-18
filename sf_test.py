import streamlit as st
from simple_salesforce import Salesforce

st.title("Salesforce Connection Test")

if st.button("Test Connection"):
    try:
        sf = Salesforce(
            username=st.secrets["salesforce"]["username"],
            password=st.secrets["salesforce"]["password"],
            security_token=st.secrets["salesforce"]["security_token"],
            domain=st.secrets["salesforce"].get("domain", "login"),  # "login" or "test"
        )

        st.success("Connected successfully.")
        st.write(f"Salesforce instance: {sf.base_url}")

    except Exception as e:
        st.error(f"Connection failed: {e}")
