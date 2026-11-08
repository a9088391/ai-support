<script setup lang="ts">
import {
  answerStatusMessages,
  ticketTime,
  type TicketDetail,
} from "~/workbench/contracts";

defineProps<{ ticket: TicketDetail }>();
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
        <dd>開啟</dd>
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
        <h4>回答狀態</h4>
        <p>{{ answerStatusMessages[ticket.answer_status] }}</p>
      </template>
      <h4>建單時已取得的搜尋結果</h4>
      <KnowledgeResultList
        v-if="ticket.search_results.length"
        :results="ticket.search_results"
      />
      <p v-else>建單時沒有可保存的搜尋結果。</p>
    </section>
  </div>
</template>
