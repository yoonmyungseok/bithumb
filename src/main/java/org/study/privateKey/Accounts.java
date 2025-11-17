package org.study.privateKey;

import lombok.Data;


/**
 * 1. ClassName     : Accounts
 * 2. FileName      : Accounts.java
 * 3. Package       : org.study.privateKey
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 16. 오후 7:53
 * 6. Modified Date : 25. 11. 16. 오후 7:53
 * 7. Comment       :
 */
@Data
public class Accounts {
    private String currency;
    private String balance;
    private String locked;
    private double avg_buy_price;
    private Boolean avg_buy_price_modified;
    private String unit_currency;
}
