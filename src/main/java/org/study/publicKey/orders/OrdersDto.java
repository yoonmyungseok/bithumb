package org.study.publicKey.orders;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 1. ClassName     : Orders
 * 2. FileName      : OrdersDto.java
 * 3. Package       : org.study.publicKey.orders
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 18. 오후 5:17
 * 6. Modified Date : 25. 11. 18. 오후 5:17
 * 7. Comment       :
 */

/**
 * 주문(Orders) 정보
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
@Builder
public class OrdersDto {
    // 주문의 고유 아이디
    @JsonProperty("uuid")
    private String uuid;
    
    // 주문 종류 (bid/ask)
    @JsonProperty("side")
    private String side;
    
    // 주문 방식 (limit, price, market 등)
    @JsonProperty("ord_type")
    private String ordType;
    
    // 주문 당시 화폐 가격 (NumberString)
    @JsonProperty("price")
    private String price;
    
    // 주문 상태 (wait, done, cancel 등)
    @JsonProperty("state")
    private String state;
    
    // 마켓의 유일키 (예: KRW-BTC)
    @JsonProperty("market")
    private String market;
    
    // 주문 생성 시간 (DateString, ISO8601)
    @JsonProperty("created_at")
    private String createdAt;
    
    // 사용자가 입력한 주문 양 (NumberString)
    @JsonProperty("volume")
    private String volume;
    
    // 체결 후 남은 주문 양 (NumberString)
    @JsonProperty("remaining_volume")
    private String remainingVolume;
    
    // 수수료로 예약된 비용 (NumberString)
    @JsonProperty("reserved_fee")
    private String reservedFee;
    
    // 남은 수수료 (NumberString)
    @JsonProperty("remaining_fee")
    private String remainingFee;
    
    // 사용된 수수료 (NumberString)
    @JsonProperty("paid_fee")
    private String paidFee;
    
    // 거래에 사용중인 비용 (NumberString)
    @JsonProperty("locked")
    private String locked;
    
    // 체결된 양 (NumberString)
    @JsonProperty("executed_volume")
    private String executedVolume;
    
    // 해당 주문에 걸린 체결 수
    @JsonProperty("trades_count")
    private Integer tradesCount;
}
