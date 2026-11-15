
from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components

from src.agent import run_agent


BASE_DIR = Path(__file__).resolve().parent

st.set_page_config(
    page_title="IA Analista de Dados",
    layout="wide"
)

st.title("IA Analista de Dados")
st.caption("Faça perguntas sobre seus dados e receba análises automatizadas.")

# Histórico da conversa
if "messages" not in st.session_state:
    st.session_state.messages = []

# Exibe as mensagens anteriores
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

        # Mostra o relatório associado à resposta
        report_path = message.get("report_path")

        if report_path and Path(report_path).exists():
            html = Path(report_path).read_text(encoding="utf-8")

            components.html(
                html,
                height=800,
                scrolling=True
            )

            st.download_button(
                label="Baixar relatório HTML",
                data=html,
                file_name=Path(report_path).name,
                mime="text/html",
                key=f"download_{len(st.session_state.messages)}"
            )


# Campo de pergunta
pergunta = st.chat_input("Ex.: Quais são as categorias mais populares?")

if pergunta:
    # Exibe e registra a pergunta
    st.session_state.messages.append({
        "role": "user",
        "content": pergunta
    })

    with st.chat_message("user"):
        st.markdown(pergunta)

    # Recupera o histórico anterior para o agente
    historico = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in st.session_state.messages[:-1]
        if msg["role"] in ("user", "assistant")
    ]

    with st.chat_message("assistant"):
        with st.spinner("Analisando os dados..."):
            try:
                # O agente deve retornar um dicionário:
                # {"answer": "...", "report_path": "..."}
                resultado = run_agent(
                    pergunta,
                    historico=historico
                )

                resposta = resultado["answer"]
                report_path = resultado.get("report_path")

                st.markdown(resposta)

                if report_path and Path(report_path).exists():
                    html = Path(report_path).read_text(encoding="utf-8")

                    components.html(
                        html,
                        height=800,
                        scrolling=True
                    )

                    st.download_button(
                        label="Baixar relatório HTML",
                        data=html,
                        file_name=Path(report_path).name,
                        mime="text/html"
                    )

                st.session_state.messages.append({
                    "role": "assistant",
                    "content": resposta,
                    "report_path": report_path
                })

            except Exception as erro:
                mensagem_erro = f"Não foi possível concluir a análise: {erro}"
                st.error(mensagem_erro)

                st.session_state.messages.append({
                    "role": "assistant",
                    "content": mensagem_erro
                })
