package org.study.common;

/**
 * 1. ClassName     : CustomException
 * 2. FileName      : CustomException.java
 * 3. Package       : org.study.common
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 18. 오후 2:14
 * 6. Modified Date : 25. 11. 18. 오후 2:14
 * 7. Comment       :
 */
public class CustomException extends RuntimeException {
    
    public CustomException(String message) {
        super(message);
    }
    
    public CustomException(String message, Throwable cause) {
        super(message, cause);
    }
}