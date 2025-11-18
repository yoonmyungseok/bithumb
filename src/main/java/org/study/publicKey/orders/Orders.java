package org.study.publicKey.orders;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 1. ClassName     : Orders
 * 2. FileName      : Orders.java
 * 3. Package       : org.study.publicKey.orders
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 18. 오후 5:39
 * 6. Modified Date : 25. 11. 18. 오후 5:39
 * 7. Comment       :
 */

@Data
@NoArgsConstructor
@AllArgsConstructor
@Builder
public class Orders {
    
    /**
     * 마켓 ID (예: KRW-BTC)
     */
    private String market;
    
    /**
     * 주문 종류
     * - bid : 매수
     * - ask : 매도
     */
    private String side;
    
    /**
     * 주문량 (지정가, 시장가 매도 시 필수)
     * NumberString
     */
    private String volume;
    
    /**
     * 주문 가격 (지정가, 시장가 매수 시 필수)
     * NumberString
     *
     * ex) KRW-BTC 마켓에서 1BTC당 1,000 KRW로 거래할 경우 값은 "1000"
     */
    private String price;
    
    /**
     * 주문 타입
     * - limit  : 지정가 주문
     * - price  : 시장가 주문(매수)
     * - market : 시장가 주문(매도)
     */
    @JsonProperty("ord_type")
    private String ordType;
}
