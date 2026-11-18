<script setup lang="ts">
import {
  answerStatusMessages,
  answerStatusTitles,
  ticketTime,
  type TicketDetail,
} from "~/workbench/contracts";

defineProps<{
  ticket: TicketDetail;
  closeRecovery: "unknown" | "conflict" | null;
}>();
</script>

<template>
  <div class="ticket-snapshot">
    <!-- 建單時編輯的描述與伺服器保存的原始問答證據分開顯示。 -->
    <dl class="knowledge-result-details">
      <div>
        <dt>工單編號</dt>
        <dd>
          <code>{{ ticket.id }}</code>
        </dd>
      </div>
      <div>
        <dt>工單狀態</dt>
        <dd v-if="closeRecovery === 'conflict'">已結案，待讀取結果</dd>
        <dd v-else-if="closeRecovery">待確認結案結果</dd>
        <dd v-else>{{ ticket.status === "closed" ? "已結案" : "開啟" }}</dd>
      </div>
      <div v-if="ticket.closed_at">
        <dt>結案時間</dt>
        <dd>
          <time :datetime="ticket.closed_at">{{
            ticketTime(ticket.closed_at)
          }}</time>
        </dd>
      </div>
      <div>
        <dt>建立時間</dt>
        <dd>
          <time :datetime="ticket.created_at">{{
            ticketTime(ticket.created_at)
          }}</time>
        </dd>
      </div>
      <div>
        <dt>建立人</dt>
        <dd>{{ ticket.created_by }}</dd>
      </div>
    </dl>

    <div class="ticket-file">
      <section
        class="knowledge-search-state"
        aria-labelledby="ticket-description-heading"
      >
        <h3 id="ticket-description-heading">問題描述</h3>
        <p class="answer-text">{{ ticket.description }}</p>
      </section>

      <section
        class="knowledge-search-state"
        aria-labelledby="ticket-evidence-heading"
      >
        <h3 id="ticket-evidence-heading">原始問答快照</h3>
        <h4>原始問題</h4>
        <p class="answer-text">{{ ticket.question }}</p>
        <template v-if="ticket.answer_status === 'answered'">
          <h4>回答</h4>
          <p class="answer-text">{{ ticket.answer }}</p>
          <h4>引用來源</h4>
          <KnowledgeResultList :results="ticket.citations" />
        </template>
        <template v-else>
          <!-- 保存的不足或技術狀態沿用工作台標題，避免回查時混淆原因。 -->
          <h4>{{ answerStatusTitles[ticket.answer_status] }}</h4>
          <p>{{ answerStatusMessages[ticket.answer_status] }}</p>
        </template>
        <details class="ticket-saved-search">
          <summary>建單時已取得的搜尋結果</summary>
          <KnowledgeResultList
            v-if="ticket.search_results.length"
            :results="ticket.search_results"
          />
          <p v-else>建單時沒有可保存的搜尋結果。</p>
        </details>
      </section>
    </div>
    <aside class="ticket-resolution" aria-label="處理結果與結案操作">
      <slot />
      <section
        v-if="ticket.status === 'closed'"
        class="knowledge-search-state"
        aria-labelledby="ticket-resolution-heading"
      >
        <h3 id="ticket-resolution-heading">處理結果</h3>
        <p class="answer-text">{{ ticket.resolution }}</p>
      </section>
    </aside>
  </div>
</template>
