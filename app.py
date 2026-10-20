import streamlit as st

st.set_page_config(page_title="NBA STATS 2.0", page_icon="🏀", layout="wide")

st.title("🏀 NBA STATS 2.0")
st.caption("Statistiche NBA e quote Bet365 Italia — versione iniziale")

st.info("App in costruzione. Le quote live saranno collegate tramite OddsPapi nei prossimi passaggi.")

scheda_partite, scheda_quote = st.tabs(["🏟️ Partite NBA", "📊 Quote giocatori"])

with scheda_partite:
    st.subheader("Calendario NBA")
    st.write("Qui visualizzeremo le partite recuperate automaticamente da OddsPapi.")
    st.caption("Nessuna partita caricata: il collegamento API non è ancora configurato.")

with scheda_quote:
    st.subheader("Quote Bet365 Italia")
    st.write("Seleziona una partita e un giocatore per visualizzare le linee disponibili.")
    mercato = st.selectbox("Mercato", ["Punti (PTS)", "Rimbalzi (REB)", "Assist (AST)", "Punti + Rimbalzi + Assist (PRA)"])
    st.caption(f"Mercato selezionato: {mercato}. Le linee Over/Under verranno mostrate qui dopo il collegamento API.")
    st.warning("Nessuna quota caricata. Non vengono mostrate quote inventate o non aggiornate.")

st.divider()
st.caption("NBA STATS 2.0 · Progetto personale")
